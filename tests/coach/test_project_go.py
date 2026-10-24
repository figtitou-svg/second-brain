from __future__ import annotations

import threading

from coach import project_assistant, store
from coach.calendar_client import FakeCalendarClient
from coach.db import Database
from coach.models import Project, Suggestion
from coach.rules import load_rules
from coach.scheduler import CoachScheduler
from tests.coach.assistant_helpers import NOW, FakeLLM, make_project, tasks_json
from tests.coach.conftest import FROZEN

GOOD = tasks_json({"title": "Alpha pipeline", "estimated_minutes": 60}, {"title": "Bravo tests"})


def cbs(buttons):
    return [cb for row in buttons for _, cb in row]


def test_go_proposes_tasks_for_the_first_open_step():
    db, p, m1, m2, m3 = make_project()
    llm = FakeLLM(GOOD)
    text, buttons = project_assistant.go(db, llm, "private", p.id, NOW)
    assert text.startswith("💡 Pour la suite de <b>Projet IA</b> (étape <i>Modèle</i>)")
    assert any(c.startswith("ps:add:") for c in cbs(buttons)) and len(llm.calls) == 1
    assert "Étape visée : Modèle" in llm.calls[0]["prompt"]


def test_go_shows_the_pending_batch_without_calling_the_llm():
    db, p, m1, m2, m3 = make_project()
    db.save_suggestions([Suggestion(batch="lot00001", project_id=p.id, milestone_id=m2.id, title="En attente",
                                    created_at=NOW)])
    llm = FakeLLM(GOOD)
    text, buttons = project_assistant.go(db, llm, "private", p.id, NOW)
    assert "En attente" in text and llm.calls == [] and any(c.startswith("ps:add:") for c in cbs(buttons))


def test_go_refuses_unknown_paused_and_finished_projects_without_calling_the_llm():
    db, p, m1, m2, m3 = make_project()
    llm = FakeLLM(GOOD)
    for status in ("paused", "done"):
        db.save_project(p.model_copy(update={"status": status}))
        text, buttons = project_assistant.go(db, llm, "private", p.id, NOW)
        assert "en pause ou terminé" in text and buttons == []
    assert "en pause ou terminé" in project_assistant.go(db, llm, "private", "zzzzzzzz", NOW)[0]
    assert llm.calls == []


def test_go_says_so_when_the_llm_is_down_or_has_no_idea():
    db, p, m1, m2, m3 = make_project()
    down, buttons = project_assistant.go(db, FakeLLM(RuntimeError("quota")), "private", p.id, NOW)
    assert "pas réussi" in down and cbs(buttons) == [f"pjv:{p.id}"]
    empty, buttons = project_assistant.go(db, FakeLLM({"tasks": []}), "private", p.id, NOW)
    assert "pas d'idée" in empty and cbs(buttons) == [f"pjv:{p.id}"]
    assert db.pending_batch(p.id) is None


def test_go_works_without_steps_or_with_every_step_crossed():
    db = Database(":memory:")
    free = Project(name="Perso", profile={"goal": "Apprendre la guitare"})
    db.save_project(free)
    llm = FakeLLM(tasks_json({"title": "Apprendre trois accords"}))
    text, _ = project_assistant.go(db, llm, "private", free.id, NOW)
    assert text.startswith("💡 Pour la suite de <b>Perso</b>") and "Étape visée : aucune" in llm.calls[0]["prompt"]
    db2, p2, a1, a2, a3 = make_project()
    for m in (a2, a3):
        db2.save_milestone(m.model_copy(update={"status": "done"}))
    llm2 = FakeLLM(tasks_json({"title": "Alpha pipeline"}))
    project_assistant.go(db2, llm2, "private", p2.id, NOW)
    assert "Étape visée : aucune" in llm2.calls[0]["prompt"]


def test_store_routes_go_outside_the_click_lock():
    db, p, m1, m2, m3 = make_project()
    sched = CoachScheduler(db, FakeCalendarClient(), load_rules(FROZEN), lambda t, b=None: None, lambda: NOW)
    store.configure(db, sched)
    store.configure_projects(FakeLLM(GOOD), "private", {})
    held, release = threading.Event(), threading.Event()

    def hold_click_lock():
        with project_assistant._CLICK_LOCK:
            held.set()
            release.wait(5)

    holder = threading.Thread(target=hold_click_lock)
    holder.start()
    assert held.wait(5)
    answer = []
    worker = threading.Thread(target=lambda: answer.append(store.assistant_action("ps", f"go:{p.id}")))
    worker.start()
    worker.join(3)
    finished = not worker.is_alive()
    release.set()
    holder.join()
    worker.join()
    assert finished and "💡" in answer[0][0]
    assert "en pause ou terminé" in store.assistant_action("ps", "go:")[0]      # argument vide : message propre
