from __future__ import annotations

from coach import project_service, store
from coach.calendar_client import FakeCalendarClient
from coach.db import Database
from coach.models import Milestone, Project, Task
from coach.rules import load_rules
from coach.scheduler import CoachScheduler
from tests.coach.conftest import FROZEN, dt

NOW = dt(2026, 10, 20, 10)


def make():
    db = Database(":memory:")
    sent = []
    s = CoachScheduler(db, FakeCalendarClient(), load_rules(FROZEN), lambda t, b=None: sent.append((t, b)),
                       lambda: NOW)
    store.configure(db, s)
    p = Project(name="Projet IA", category="IA")
    db.save_project(p)
    m = Milestone(project_id=p.id, title="Modèle", position=0)
    db.save_milestone(m)
    a = Task(title="[IA] a", project_id=p.id, milestone_id=m.id, status="scheduled")
    b = Task(title="[IA] b", project_id=p.id, milestone_id=m.id, status="pending")
    db.upsert_task(a)
    db.upsert_task(b)
    return db, s, sent, p, m, a, b


def prompts(sent):
    return [(t, b) for t, b in sent if "Franchie" in t]


def test_prompt_only_when_the_last_open_task_of_the_milestone_is_done():
    db, s, sent, p, m, a, b = make()
    store.mark_done(a.id)
    assert prompts(sent) == []                       # il reste une tâche ouverte
    store.mark_done(b.id)
    (text, buttons), = prompts(sent)
    assert "Modèle" in text and "Projet IA" in text
    assert buttons == [[("Oui", f"pm:ok:{m.id}"), ("Pas encore", f"pm:no:{m.id}")]]
    assert db.get_project(p.id).last_activity == NOW


def test_asked_once_then_again_after_new_tasks_are_finished():
    db, s, sent, p, m, a, b = make()
    store.mark_done(a.id)
    store.mark_done(b.id)
    assert project_service.after_task_done(db, b.id, NOW) is None     # même nombre de tâches faites : déjà demandé
    c = Task(title="[IA] c", project_id=p.id, milestone_id=m.id, status="pending")
    db.upsert_task(c)
    store.mark_done(c.id)
    assert len(prompts(sent)) == 2


def test_no_prompt_for_paused_done_or_unlinked_cases():
    db, s, sent, p, m, a, b = make()
    db.save_project(p.model_copy(update={"status": "paused"}))
    store.mark_done(a.id)
    store.mark_done(b.id)
    assert prompts(sent) == [] and db.get_project(p.id).last_activity == NOW   # activité mise à jour malgré la pause
    db.save_project(p.model_copy(update={"status": "active"}))
    db.save_milestone(m.model_copy(update={"status": "done"}))
    c = Task(title="[IA] c", project_id=p.id, milestone_id=m.id, status="done")
    db.upsert_task(c)
    assert project_service.after_task_done(db, c.id, NOW) is None             # étape déjà franchie
    loose = Task(title="[IA] d", project_id=p.id, status="done")
    db.upsert_task(loose)
    assert project_service.after_task_done(db, loose.id, NOW) is None         # tâche sans étape
    free = Task(title="libre", status="done")
    db.upsert_task(free)
    assert project_service.after_task_done(db, free.id, NOW) is None          # tâche sans projet
    assert project_service.after_task_done(db, "inconnue", NOW) is None


def test_dropped_tasks_do_not_block_the_prompt():
    db, s, sent, p, m, a, b = make()
    db.set_task_status(b.id, "dropped")
    store.mark_done(a.id)
    assert len(prompts(sent)) == 1


def test_block_completion_path_also_prompts():
    db, s, sent, p, m, a, b = make()
    db.set_task_status(b.id, "done")
    blk_task = a
    from coach.models import Block
    blk = Block(task_id=blk_task.id, start=dt(2026, 10, 20, 9), end=dt(2026, 10, 20, 10))
    db.add_block(blk)
    s.mark_done(blk.id)                              # bouton « C'est fait ? » du créneau
    assert len(prompts(sent)) == 1


def test_a_failure_in_the_prompt_never_breaks_task_completion(monkeypatch):
    db, s, sent, p, m, a, b = make()

    def boom(*args, **kwargs):
        raise RuntimeError("base verrouillée")

    monkeypatch.setattr(project_service, "after_task_done", boom)
    assert store.mark_done(a.id) is True and db.get_task(a.id).status == "done"
