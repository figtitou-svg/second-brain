"""Réglages « vie plus facile » : tâches de mails à confirmer, vue Maintenant, faites, annulation, étape planifiée."""
from datetime import timedelta

import pytest

from bot.fakes import FakeCoach, FakeMemory, FakeRouter
from bot.handlers import BotService
from coach import store
from coach.calendar_client import FakeCalendarClient
from coach.db import Database
from coach.models import Block, Milestone, Project, Task
from coach.rules import load_rules
from coach.scheduler import CoachScheduler
from tests.coach.conftest import FROZEN, dt
from web import data

NOW = dt(2026, 11, 11, 9, 0)


@pytest.fixture
def env():
    db = Database(":memory:")
    sched = CoachScheduler(db, FakeCalendarClient(), load_rules(FROZEN), lambda t, b: None, now_fn=lambda: NOW)
    store.configure(db, sched)
    return db, sched


def test_mail_task_waits_in_a_trier_until_kept(env):
    db, _ = env
    t = Task(title="Répondre au sondage", source_id="insa:mid:1")
    db.upsert_task(t)
    db.add_mail_origin(t.id, "asso@insa-lyon.fr", NOW)
    assert db.list_tasks("pending", "scheduled") == [] and [x.id for x in db.proposed_tasks()] == [t.id]
    assert [x.id for x in db.list_tasks()] == [t.id]                 # toujours connue (anti-doublon)
    assert data.task_groups(db, NOW)[0] == []                        # pas dans Tâches
    assert db.sender_verdicts("asso@insa-lyon.fr") == (0, 0)         # en attente : ni refus ni gardée
    assert store.confirm(t.id) and not store.confirm(t.id)
    assert [x.id for x in db.list_tasks("pending")] == [t.id] and db.proposed_tasks() == []
    assert db.sender_verdicts("asso@insa-lyon.fr") == (0, 1)         # gardée : compte pour l'expéditeur


def test_ignore_all_proposed_and_undo(env):
    db, _ = env
    kept = Task(title="Normale")
    db.upsert_task(kept)
    for i in range(3):
        t = Task(title=f"Proposée {i}", source_id=f"insa:mid:{i}")
        db.upsert_task(t)
        db.add_mail_origin(t.id, "x@insa-lyon.fr", NOW)
    assert store.ignore_proposed() == 3 and db.proposed_tasks() == []
    assert db.get_task(kept.id).status == "pending"                  # les autres tâches ne sont pas touchées
    assert db.sender_verdicts("x@insa-lyon.fr") == (3, 0)
    store.mark_done(kept.id)
    assert "de retour" in store.restore_task(kept.id) and db.get_task(kept.id).status == "pending"
    assert "déjà dans la liste" in store.restore_task(kept.id)


def test_callbacks_keep_undo_and_confirming_actions():
    coach = FakeCoach()
    coach.tasks["t1"] = Task(id="t1", title="Lire le poly")
    svc = BotService(coach, FakeMemory(), FakeRouter(), 1)
    assert "gardée" in svc.on_callback("keep:t1").text
    done = svc.on_callback("td:t1")
    assert done.buttons == [[("↩ Annuler", "undo:t1")]]
    assert "Annulé" in svc.on_callback("undo:t1").text and coach.tasks["t1"].status == "pending"
    assert svc.on_callback("ign:t1").buttons == [[("↩ Annuler", "undo:t1")]]
    assert "Étape planifiée" in svc.on_callback("pls:m1").text
    assert svc.on_callback("ignall").text == "Rien à ignorer."


def test_now_view_today_week_later_and_only_next_task_per_project(env):
    db, _ = env
    p = Project(name="Security+")
    db.save_project(p)
    m = Milestone(project_id=p.id, title="Domaine 1", position=0)
    db.save_milestone(m)
    slot = Task(title="A créneau ce soir", project_id=p.id, milestone_id=m.id, status="scheduled")
    week = Task(title="B dans 3 jours", due=NOW + timedelta(days=3))
    late = Task(title="C en retard", due=NOW - timedelta(days=1))
    for t in (slot, week, late, *[Task(title=f"D{i}", project_id=p.id, milestone_id=m.id) for i in range(4)],
              Task(title="E libre sans date")):
        db.upsert_task(t)
    db.add_block(Block(task_id=slot.id, start=NOW + timedelta(hours=9), end=NOW + timedelta(hours=10)))
    groups, _ = data.task_groups(db, NOW)
    by = {g.key: g for g in groups}
    assert [g.key for g in groups] == ["now:today", "now:week", "now:later"]
    assert [r.title for r in by["now:today"].rows] == ["C en retard", "A créneau ce soir"]
    assert by["now:today"].rows[1].slot == NOW + timedelta(hours=9) and by["now:today"].open
    assert [r.title for r in by["now:week"].rows] == ["B dans 3 jours"]
    later = by["now:later"].rows
    assert sorted(r.title for r in later) == ["D0", "E libre sans date"]            # un seul D : le prochain
    assert next(r for r in later if r.title == "D0").more == 3 and not by["now:later"].open
    proj, _ = data.task_groups(db, NOW, "tout", "projet")                             # la vue par projet garde tout
    assert len(proj[0].rows) == 5 and proj[0].next == "A créneau ce soir"


def test_done_filter_lists_finished_tasks(env):
    db, _ = env
    db.upsert_task(Task(title="Finie", status="done"))
    db.upsert_task(Task(title="Ouverte"))
    groups, counts = data.task_groups(db, NOW, "faites")
    assert counts["faites"] == 1 and counts["tout"] == 1
    assert [r.title for r in groups[0].rows] == ["Finie"] and groups[0].rows[0].status == "done"


def test_schedule_step_places_every_unplanned_task_and_long_tasks_are_refused(env):
    db, _ = env
    p = Project(name="Cyber")
    db.save_project(p)
    m = Milestone(project_id=p.id, title="Attaque", position=0)
    db.save_milestone(m)
    for i in range(3):
        db.upsert_task(Task(title=f"Notion {i}", project_id=p.id, milestone_id=m.id, estimated_minutes=30))
    out = store.schedule_step(m.id)
    assert "3 tâches planifiées" in out
    assert all(t.status == "scheduled" for t in db.list_project_tasks(p.id))
    assert "Rien à planifier" in store.schedule_step(m.id)
    exam = Task(title="Passer l'examen", estimated_minutes=2880)
    db.upsert_task(exam)
    assert "trop long" in store.schedule(exam.id) and db.list_blocks(exam.id, open_only=True) == []


def test_home_ring_shows_the_step(env):
    db, sched = env
    p = Project(name="Cyber", last_activity=NOW)
    db.save_project(p)
    for i, st in enumerate(("done", "done", "open", "open")):
        db.save_milestone(Milestone(project_id=p.id, title=f"É{i}", position=i, status=st))
    home = data.build_home(db, sched, data.AgendaCache(lambda s, e: []), NOW)
    assert home.rings[0].step == "Étape 3/4"
