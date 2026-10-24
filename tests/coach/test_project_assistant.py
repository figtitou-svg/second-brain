from __future__ import annotations

import threading
import time

from coach import project_assistant
from coach.db import Database
from coach.models import Project, Suggestion, Task
from tests.coach.assistant_helpers import NOW, FakeLLM, make_project, tasks_json


def test_prompt_has_the_sheet_the_steps_and_the_history():
    db, p, m1, m2, m3 = make_project()
    db.upsert_task(Task(title="[IA] Nettoyer le dataset", project_id=p.id, milestone_id=m1.id, status="done"))
    db.upsert_task(Task(title="[IA] Choisir le modèle", project_id=p.id, milestone_id=m2.id, status="pending"))
    db.save_suggestions([Suggestion(batch="b1", project_id=p.id, title="Écrire le README", state="dismissed",
                                    created_at=NOW)])
    prompt = project_assistant.build_prompt(db, p, m2, NOW)
    for expected in ("Projet : Projet IA", "Catégorie : IA", "Objectif : Entraîner un modèle",
                     "Limites : Pas de deep learning", "Rendu : 2026-12-15", "faite : Données",
                     "ouverte : Modèle (avant 2026-11-20)", "Étape visée : Modèle", "Nettoyer le dataset",
                     "Choisir le modèle", "Écrire le README (écartée)"):
        assert expected in prompt


def test_suggest_saves_a_batch_with_clean_fields():
    db, p, m1, m2, m3 = make_project()
    llm = FakeLLM(tasks_json(
        {"title": "Entraîner un premier modèle", "estimated_minutes": 90, "milestone": "modèle"},
        {"title": "Mesurer la précision", "estimated_minutes": 2, "milestone": "Étape inconnue"},
        {"title": "Rédiger la méthode", "estimated_minutes": "9999", "milestone": "Rapport"},
        {"title": "Planifier la soutenance"}))
    out = project_assistant.suggest(db, llm, "private", p, m2, NOW)
    assert [s.title for s in out] == ["Entraîner un premier modèle", "Mesurer la précision", "Rédiger la méthode",
                                      "Planifier la soutenance"]
    assert len({s.batch for s in out}) == 1 and all(s.state == "proposed" for s in out)
    assert [s.estimated_minutes for s in out] == [90, 5, 480, 30]
    assert [s.milestone_id for s in out] == [m2.id, m2.id, m3.id, m2.id]
    assert [s.id for s in db.list_batch(out[0].batch)] == [s.id for s in out]
    call = llm.calls[0]
    assert call["sensitivity"] == "private" and call["json_mode"] is True and "JSON" in call["system"]


def test_filters_duplicates_empty_titles_and_caps_at_five():
    db, p, m1, m2, m3 = make_project()
    db.upsert_task(Task(title="[IA] Nettoyer le dataset", project_id=p.id, status="done"))
    db.save_suggestions([Suggestion(batch="old", project_id=p.id, title="Écrire le README", state="added",
                                    created_at=NOW)])
    items = ([{"title": "Nettoyer le dataset"}, {"title": "  "}, {"title": False}, {"title": "Écrire le README"},
              {"title": "A" * 200}, {"title": "Tâche 1 unique"}, {"title": "Tâche 1 unique"}]
             + [{"title": t} for t in ("Alpha pipeline", "Bravo tests", "Charlie deploy", "Delta docs")])
    out = project_assistant.suggest(db, FakeLLM(tasks_json(*items)), "private", p, m2, NOW)
    titles = [s.title for s in out]
    assert "Nettoyer le dataset" not in titles and "Écrire le README" not in titles
    assert titles[0] == "A" * 80 and titles.count("Tâche 1 unique") == 1
    assert titles == ["A" * 80, "Tâche 1 unique", "Alpha pipeline", "Bravo tests", "Charlie deploy"]


def test_non_finite_or_non_positive_minutes_never_raise():
    db, p, m1, m2, m3 = make_project()
    raw = ('{"tasks": [{"title": "Alpha pipeline", "estimated_minutes": Infinity},'
           ' {"title": "Bravo tests", "estimated_minutes": NaN},'
           ' {"title": "Charlie deploy", "estimated_minutes": "1e999"},'
           ' {"title": "Delta docs", "estimated_minutes": 0},'
           ' {"title": "Echo review", "estimated_minutes": -20}]}')
    out = project_assistant.suggest(db, FakeLLM(raw), "private", p, m2, NOW)
    assert [s.title for s in out] == ["Alpha pipeline", "Bravo tests", "Charlie deploy", "Delta docs", "Echo review"]
    assert [s.estimated_minutes for s in out] == [30, 30, 30, 5, 5]


def test_prompt_lists_only_the_last_open_tasks():
    db, p, m1, m2, m3 = make_project()
    for i in range(12):
        # id croissant : la date de création est à la seconde, l'ordre des tâches créées ensemble suit l'id
        db.upsert_task(Task(id=f"t{i:02d}", title=f"[IA] Ouverte numero{i:02d}", project_id=p.id, status="pending"))
    prompt = project_assistant.build_prompt(db, p, m2, NOW)
    assert "numero00" not in prompt and "numero01" not in prompt
    assert all(f"numero{i:02d}" in prompt for i in range(2, 12))


def test_failures_return_none_and_valid_but_empty_returns_empty_list():
    db, p, m1, m2, m3 = make_project()
    assert project_assistant.suggest(db, FakeLLM(RuntimeError("quota")), "private", p, m2, NOW) is None
    assert project_assistant.suggest(db, FakeLLM("pas du json", "encore pas"), "private", p, m2, NOW) is None
    assert project_assistant.suggest(db, None, "private", p, m2, NOW) is None
    assert project_assistant.suggest(db, FakeLLM({"tasks": []}), "private", p, m2, NOW) == []
    assert project_assistant.suggest(db, FakeLLM(tasks_json({"title": 5}, {"title": ""})), "private", p, m2, NOW) == []
    assert db.pending_batch(p.id) is None


def test_invalid_json_gets_one_retry():
    db, p, m1, m2, m3 = make_project()
    llm = FakeLLM("pas du json", tasks_json({"title": "Alpha pipeline"}))
    out = project_assistant.suggest(db, llm, "private", p, m2, NOW)
    assert [s.title for s in out] == ["Alpha pipeline"] and len(llm.calls) == 2
    assert "invalide" in llm.calls[1]["prompt"]


def test_pending_batch_blocks_a_new_one_without_calling_the_llm():
    db, p, m1, m2, m3 = make_project()
    db.save_suggestions([Suggestion(batch="b1", project_id=p.id, title="En attente", created_at=NOW)])
    llm = FakeLLM(tasks_json({"title": "Alpha pipeline"}))
    assert project_assistant.suggest(db, llm, "private", p, m2, NOW) == []
    assert llm.calls == []


def test_project_without_steps_has_no_target_step():
    db = Database(":memory:")
    p = Project(name="Perso", profile={"goal": "Apprendre la guitare"})
    db.save_project(p)
    llm = FakeLLM(tasks_json({"title": "Apprendre trois accords", "milestone": "Inconnue"}))
    out = project_assistant.suggest(db, llm, "private", p, None, NOW)
    assert out[0].milestone_id is None and "Étape visée : aucune" in llm.calls[0]["prompt"]


def test_two_simultaneous_generations_make_one_batch():
    db, p, m1, m2, m3 = make_project()

    class SlowLLM(FakeLLM):
        def __call__(self, *a, **k):
            time.sleep(0.2)
            return super().__call__(*a, **k)

    llm = SlowLLM(tasks_json({"title": "Alpha pipeline"}, {"title": "Bravo tests"}, {"title": "Charlie deploy"}))
    results = []
    threads = [threading.Thread(target=lambda: results.append(project_assistant.suggest(db, llm, "private", p, m2, NOW)))
               for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(llm.calls) == 1
    assert len({s.batch for s in db.list_project_suggestions(p.id)}) == 1
    assert sorted(len(r) for r in results) == [0, 3]
