from __future__ import annotations

from coach import project_assistant, project_service
from coach.models import Task
from tests.coach.assistant_helpers import NOW, FakeLLM, make_project, tasks_json


def cbs(buttons):
    return [cb for row in buttons for _, cb in row]


def item(title, step, minutes=60):
    return {"title": title, "estimated_minutes": minutes, "milestone": step}


def test_gaps_are_open_steps_without_an_open_task():
    db, p, _, modele, rapport = make_project()
    assert [m.id for m in project_service.step_gaps(db, p.id)] == [modele.id, rapport.id]
    db.upsert_task(Task(title="[IA] Entraîner", project_id=p.id, milestone_id=modele.id))
    assert [m.id for m in project_service.step_gaps(db, p.id)] == [rapport.id]


def test_button_only_when_a_step_has_nothing_to_do():
    db, p, _, modele, rapport = make_project()
    assert f"pa:mk:{p.id}" in cbs(project_service.detail(db, p.id, NOW)[1])
    for m in (modele, rapport):
        db.upsert_task(Task(title=f"[IA] t{m.position}", project_id=p.id, milestone_id=m.id))
    assert f"pa:mk:{p.id}" not in cbs(project_service.detail(db, p.id, NOW)[1])
    db.set_project_status(p.id, "paused") if hasattr(db, "set_project_status") else None


def test_creates_tasks_directly_in_their_steps_with_the_category_prefix_and_due_date():
    db, p, _, modele, rapport = make_project()
    llm = FakeLLM(tasks_json(item("Entraîner le modèle", "Modèle"), item("Rédiger le rapport", "Rapport", 120)))
    text, buttons = project_assistant.fill_steps(db, llm, "private", p.id, NOW)
    tasks = {t.milestone_id: t for t in db.list_project_tasks(p.id)}
    assert tasks[modele.id].title == "[IA] Entraîner le modèle" and tasks[modele.id].due.date() == modele.due
    assert tasks[rapport.id].estimated_minutes == 120 and tasks[rapport.id].due.date() == p.deadline
    assert "2 tâche(s) créée(s)" in text and f"pjv:{p.id}" in cbs(buttons)
    assert "Étapes à couvrir" in llm.calls[0]["prompt"] and "Données" not in llm.calls[0]["prompt"].split("Étapes à couvrir")[1]


def test_nothing_is_created_and_no_ai_call_when_every_step_has_a_task():
    db, p, _, modele, rapport = make_project()
    for m in (modele, rapport):
        db.upsert_task(Task(title=f"[IA] t{m.position}", project_id=p.id, milestone_id=m.id))
    llm = FakeLLM()
    text, _ = project_assistant.fill_steps(db, llm, "private", p.id, NOW)
    assert "rien à créer" in text and llm.calls == [] and len(db.list_project_tasks(p.id)) == 2


def test_nothing_is_created_when_the_ai_has_nothing_useful():
    db, p, *_ = make_project()
    text, _ = project_assistant.fill_steps(db, FakeLLM(tasks_json()), "private", p.id, NOW)
    assert "pas trouvé" in text and db.list_project_tasks(p.id) == []


def test_unknown_or_covered_steps_and_duplicates_are_ignored_and_the_cap_per_step_holds():
    db, p, _, modele, rapport = make_project()
    db.upsert_task(Task(title="[IA] Rapport déjà là", project_id=p.id, milestone_id=rapport.id))
    llm = FakeLLM(tasks_json(item("Une tâche pour Données", "Données"), item("Étape inventée", "Nulle part"),
                             item("Pour le rapport", "Rapport"), item("A", "Modèle"), item("B", "Modèle"),
                             item("C", "Modèle"), item("D", "Modèle"), item("A", "Modèle")))
    project_assistant.fill_steps(db, llm, "private", p.id, NOW)
    titles = sorted(t.title for t in db.list_project_tasks(p.id) if t.milestone_id == modele.id)
    assert titles == ["[IA] A", "[IA] B", "[IA] C"]
    assert all(t.milestone_id != rapport.id or t.title == "[IA] Rapport déjà là" for t in db.list_project_tasks(p.id))
    assert len(db.list_project_tasks(p.id)) == 4


def test_second_click_creates_nothing_more():
    db, p, *_ = make_project()
    llm = FakeLLM(tasks_json(item("Entraîner", "Modèle"), item("Rédiger", "Rapport")))
    project_assistant.fill_steps(db, llm, "private", p.id, NOW)
    text, _ = project_assistant.fill_steps(db, llm, "private", p.id, NOW)
    assert "rien à créer" in text and len(llm.calls) == 1 and len(db.list_project_tasks(p.id)) == 2


def test_ai_down_creates_nothing_and_paused_project_is_left_alone():
    db, p, *_ = make_project()
    text, _ = project_assistant.fill_steps(db, FakeLLM(RuntimeError("quota")), "private", p.id, NOW)
    assert "pas réussi" in text and db.list_project_tasks(p.id) == []
    db.save_project(p.model_copy(update={"status": "paused"}))
    text, _ = project_assistant.fill_steps(db, FakeLLM(), "private", p.id, NOW)
    assert "pause" in text


def test_store_routes_the_button():
    from coach import store
    from coach.calendar_client import FakeCalendarClient
    from coach.rules import load_rules
    from coach.scheduler import CoachScheduler
    from tests.coach.conftest import FROZEN

    db, p, _, modele, _ = make_project()
    store.configure(db, CoachScheduler(db, FakeCalendarClient(), load_rules(FROZEN), lambda t, b=None: None, lambda: NOW))
    store.configure_projects(FakeLLM(tasks_json(item("Entraîner", "Modèle"))), "private", {})
    text, _ = store.assistant_action("pa", f"mk:{p.id}")
    assert "1 tâche(s) créée(s)" in text and db.list_project_tasks(p.id)[0].milestone_id == modele.id
