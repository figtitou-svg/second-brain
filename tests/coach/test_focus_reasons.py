from __future__ import annotations

import json
import time

from coach import focus
from coach.db import Database
from coach.models import Task
from tests.coach.conftest import dt

NOW = dt(2026, 10, 15, 9, 0)  # jeudi


def T(title="t", minutes=60, due=None, **kw) -> Task:
    return Task(title=title, estimated_minutes=minutes, due=due, **kw)


def rk(tasks):
    return focus.rank_tasks(tasks, {}, NOW, {}, 180)


class FakeLLM:
    def __init__(self, answer=None, error=None, delay=0.0):
        self.answer, self.error, self.delay, self.calls = answer, error, delay, []

    def __call__(self, prompt, *, sensitivity, system=None, json_mode=False):
        self.calls.append({"prompt": prompt, "sensitivity": sensitivity, "json_mode": json_mode})
        if self.delay:
            time.sleep(self.delay)
        if self.error:
            raise self.error
        return self.answer


def test_fallback_reason_mentions_deadline_and_reschedules():
    r = rk([T("[IA] Rendu", due=dt(2026, 10, 16, 18), reschedule_count=2)])[0]
    txt = focus.fallback_reason(r)
    assert "rendu dans 1 jour" in txt and "reportée 2 fois" in txt


def test_fallback_reason_other_cases():
    late = rk([T("a", due=dt(2026, 10, 14, 18))])[0]
    assert focus.fallback_reason(late) == "en retard"
    none = rk([T("b")])[0]
    assert focus.fallback_reason(none) == "sans échéance"
    boosted = rk([T("c", due=dt(2026, 10, 25, 18), boost=2)])[0]
    assert "tu l'as marquée importante" in focus.fallback_reason(boosted)
    today = rk([T("d", minutes=10, due=dt(2026, 10, 15, 23, 59), not_before=dt(2026, 10, 15))])[0]
    assert focus.fallback_reason(today) == "à faire aujourd'hui"


def test_reasons_use_the_llm_phrase_when_it_answers():
    ranked = rk([T("Rendu IA", due=dt(2026, 10, 16, 18))])
    tid = ranked[0].task.id
    llm = FakeLLM(json.dumps({tid: "Phrase naturelle."}))
    out = focus.reasons(ranked, llm, None, "private")
    assert out[tid] == "Phrase naturelle."
    call = llm.calls[0]
    assert call["sensitivity"] == "private" and call["json_mode"] is True
    assert "Rendu IA" in call["prompt"] and tid in call["prompt"]


def test_reasons_fall_back_when_the_llm_fails_or_is_invalid():
    ranked = rk([T("a", due=dt(2026, 10, 16, 18))])
    expected = {ranked[0].task.id: focus.fallback_reason(ranked[0])}
    assert focus.reasons(ranked, FakeLLM(error=RuntimeError("quota")), None) == expected
    assert focus.reasons(ranked, FakeLLM(answer="pas du json"), None) == expected
    assert focus.reasons(ranked, FakeLLM(answer="[1, 2]"), None) == expected


def test_reasons_fall_back_on_timeout():
    ranked = rk([T("a", due=dt(2026, 10, 16, 18))])
    llm = FakeLLM(answer="{}", delay=0.4)
    out = focus.reasons(ranked, llm, None, timeout=0.05)
    assert out == {ranked[0].task.id: focus.fallback_reason(ranked[0])}


def test_partial_answer_keeps_fallback_for_missing_tasks():
    ranked = rk([T("a", due=dt(2026, 10, 16, 18)), T("b", due=dt(2026, 10, 17, 18))])
    a, b = ranked[0].task.id, ranked[1].task.id
    out = focus.reasons(ranked, FakeLLM(json.dumps({a: "Juste celle-ci."})), None)
    assert out[a] == "Juste celle-ci." and out[b] == focus.fallback_reason(ranked[1])


def test_second_identical_call_is_served_by_the_cache():
    db = Database(":memory:")
    ranked = rk([T("a", due=dt(2026, 10, 16, 18))])
    llm = FakeLLM(json.dumps({ranked[0].task.id: "En cache."}))
    assert focus.reasons(ranked, llm, db)[ranked[0].task.id] == "En cache."
    assert focus.reasons(ranked, llm, db)[ranked[0].task.id] == "En cache."
    assert len(llm.calls) == 1


def test_without_llm_or_tasks():
    ranked = rk([T("a", due=dt(2026, 10, 16, 18))])
    assert focus.reasons(ranked, None, None) == {ranked[0].task.id: focus.fallback_reason(ranked[0])}
    assert focus.reasons([], FakeLLM("{}"), None) == {}


def test_render_empty():
    assert "journée libre" in focus.render([], {}, NOW)


def test_render_top_three_and_rest_summary():
    ranked = rk([T(f"t{i}", due=dt(2026, 10, 16 + i, 18)) for i in range(5)])
    text = focus.render(ranked, focus.reasons(ranked, None, None), NOW)
    assert text.startswith("🎯 <b>Ton focus</b>")
    assert text.count("   <i>") == 3 and "+ 2 autres" in text and "dont" not in text


def test_render_counts_rest_due_before_sunday():
    ranked = rk([T(f"t{i}", due=dt(2026, 10, d, 18)) for i, d in enumerate([16, 16, 16, 17, 17])])
    text = focus.render(ranked, focus.reasons(ranked, None, None), NOW)
    assert "+ 2 autres, dont 2 avant dimanche" in text


def test_render_now_mode_marks_the_first_task():
    ranked = rk([T("a", due=dt(2026, 10, 16, 18))])
    text = focus.render(ranked, focus.reasons(ranked, None, None), NOW, now_mode=True,
                        extra_line="⏳ Tu as 1 h 20 avant « TD »")
    assert "À faire maintenant" in text and "⏳ Tu as 1 h 20" in text


def test_buttons_one_row_per_focus_task():
    ranked = rk([T(f"t{i}", due=dt(2026, 10, 16 + i, 18)) for i in range(5)])
    rows = focus.focus_buttons(ranked)
    assert len(rows) == 3
    first = ranked[0].task.id
    assert rows[0] == [("✅ 1", f"td:{first}"), ("⬆ 1", f"fup:{first}"), ("⬇ 1", f"fdn:{first}")]
