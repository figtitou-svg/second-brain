from __future__ import annotations

import json
from datetime import timedelta

import pytest

from coach.db import Database
from coach.extract import ExtractCache, extract_tasks
from tests.coach.conftest import dt

NOW = dt(2026, 10, 15, 9, 0)


class FakeLLM:
    def __init__(self, *responses: str) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []

    def __call__(self, prompt, *, sensitivity, system=None, json_mode=False):
        self.calls.append({"prompt": prompt, "sensitivity": sensitivity, "json_mode": json_mode})
        return self.responses.pop(0)


def payload(*tasks: dict) -> str:
    return json.dumps({"tasks": list(tasks)})


GOOD = {"title": "Envoyer le devis au BdE", "due": "2026-10-17T18:00:00+02:00",
        "estimated_minutes": 60, "priority": "high", "confidence": 0.82}


def run(llm, text="mail", cache=None, sens="private", source="gmail:1"):
    return extract_tasks(text, source, sens, llm=llm, cache=cache or ExtractCache(), now=NOW)


def test_valid_extraction():
    llm = FakeLLM(payload(GOOD))
    tasks = run(llm)
    assert len(tasks) == 1
    t = tasks[0]
    assert t.title == "Envoyer le devis au BdE" and t.priority == "high"
    assert t.estimated_minutes == 60 and t.source_id == "gmail:1"
    assert t.due == dt(2026, 10, 17, 18)
    assert llm.calls[0]["sensitivity"] == "private" and llm.calls[0]["json_mode"] is True


def test_sensitivity_forwarded_public():
    llm = FakeLLM(payload())
    run(llm, sens="public")
    assert llm.calls[0]["sensitivity"] == "public"


def test_low_confidence_ignored():
    low = {**GOOD, "title": "peut-être", "confidence": 0.59}
    edge = {**GOOD, "title": "limite", "confidence": 0.6}
    tasks = run(FakeLLM(payload(low, edge)))
    assert [t.title for t in tasks] == ["limite"]


def test_due_null_stays_none_so_it_is_placed_asap():
    tasks = run(FakeLLM(payload({**GOOD, "due": None})))
    assert tasks[0].due is None


def test_naive_due_is_paris():
    tasks = run(FakeLLM(payload({**GOOD, "due": "2026-10-17T18:00:00"})))
    assert tasks[0].due == dt(2026, 10, 17, 18)


def test_code_fence_and_noise_tolerated():
    raw = "Voici :\n```json\n" + payload(GOOD) + "\n```"
    assert len(run(FakeLLM(raw))) == 1
    assert len(run(FakeLLM("blabla " + payload(GOOD) + " fin"), text="autre")) == 1


def test_one_retry_then_success():
    llm = FakeLLM("pas du json", payload(GOOD))
    assert len(run(llm)) == 1
    assert len(llm.calls) == 2
    assert "invalide" in llm.calls[1]["prompt"]


def test_two_failures_abandon_and_log(caplog):
    llm = FakeLLM("nope", '{"tasks": [{"title": ""}]}', payload(GOOD))
    with caplog.at_level("ERROR", logger="coach.extract"):
        assert run(llm) == []
    assert len(llm.calls) == 2  # un seul retry
    assert any("abandonnée" in r.message for r in caplog.records)


def test_invalid_priority_fails_validation():
    llm = FakeLLM(payload({**GOOD, "priority": "urgent"}), payload({**GOOD, "priority": "urgent"}))
    assert run(llm) == []


def test_empty_tasks():
    assert run(FakeLLM('{"tasks": []}')) == []


def test_cache_prevents_second_llm_call():
    cache = ExtractCache()
    llm = FakeLLM(payload(GOOD))
    first = run(llm, cache=cache)
    second = run(llm, cache=cache)  # FakeLLM n'a plus de réponse : un appel lèverait
    assert [t.id for t in first] == [t.id for t in second]
    assert len(llm.calls) == 1


def test_cache_key_depends_on_sensitivity_and_text():
    cache = ExtractCache()
    llm = FakeLLM(payload(GOOD), payload(GOOD), payload(GOOD))
    run(llm, text="a", cache=cache)
    run(llm, text="b", cache=cache)
    run(llm, text="a", cache=cache, sens="public")
    assert len(llm.calls) == 3


def test_failure_not_cached():
    cache = ExtractCache()
    run(FakeLLM("x", "y"), cache=cache)
    llm = FakeLLM(payload(GOOD))
    assert len(run(llm, cache=cache)) == 1


def test_cache_in_sqlite():
    db = Database(":memory:")
    llm = FakeLLM(payload(GOOD))
    run(llm, cache=ExtractCache(db))
    assert len(run(llm, cache=ExtractCache(db))) == 1
    assert len(llm.calls) == 1


def test_deterministic_task_id():
    a = run(FakeLLM(payload(GOOD)), text="1")[0]
    b = run(FakeLLM(payload(GOOD)), text="2")[0]
    assert a.id == b.id


def test_llm_exceptions_propagate():
    class SensitivityError(Exception): ...

    def boom(*a, **k):
        raise SensitivityError("cloud interdit")

    with pytest.raises(SensitivityError):
        extract_tasks("x", "s", "private", llm=boom, cache=ExtractCache(), now=NOW)


def test_estimated_minutes_capped():
    t = run(FakeLLM(payload({**GOOD, "estimated_minutes": 5000})))[0]
    assert t.estimated_minutes == 480


def test_task_on_a_given_day_is_not_planned_before():
    # « prendre les chaussures de sécu vendredi » : à faire CE jour-là, pas avant
    shoes = {"title": "Prendre les chaussures de sécu", "do_on": "2026-10-16", "due": None,
             "estimated_minutes": 10, "priority": "medium", "confidence": 0.9}
    t = run(FakeLLM(payload(shoes)))[0]
    assert t.not_before == dt(2026, 10, 16)
    assert t.due.date().isoformat() == "2026-10-16" and t.due.hour == 23


def test_deadline_task_has_no_start_day():
    # « rendre le rapport avant vendredi » : une échéance, faisable dès maintenant
    t = run(FakeLLM(payload(GOOD)))[0]
    assert t.not_before is None


def test_prompt_explains_day_vs_deadline():
    from coach.extract import SYSTEM_PROMPT
    assert "do_on" in SYSTEM_PROMPT and "avant vendredi" in SYSTEM_PROMPT


@pytest.mark.parametrize("given,expected", [(1, 5), (7, 5), (8, 10), (12, 10), (13, 15), (37, 30), (38, 45),
                                            (52, 45), (53, 60), (75, 90), (76, 90), (150, 180), (151, 180),
                                            (200, 180), (210, 240), (5000, 480)])
def test_estimated_minutes_snapped_to_scale(given, expected):
    t = run(FakeLLM(payload({**GOOD, "estimated_minutes": given})))[0]
    assert t.estimated_minutes == expected


def test_prompt_lists_the_duration_scale():
    from coach.extract import DURATION_SCALE, SYSTEM_PROMPT
    assert all(str(m) in SYSTEM_PROMPT for m in DURATION_SCALE)
    assert "formulaire" in SYSTEM_PROMPT


def test_prompt_sets_a_floor_for_forms_with_documents():
    from coach.extract import SYSTEM_PROMPT
    assert "au moins 60" in SYSTEM_PROMPT and "stage" in SYSTEM_PROMPT
