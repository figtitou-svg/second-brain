"""Tâche sans date planifiée d'office ; changer la durée d'une tâche planifiée la replace."""
from bot.fakes import FakeCoach, FakeMemory, FakeRouter, FakeTask
from bot.handlers import BotService
from coach import store
from coach.calendar_client import FakeCalendarClient
from coach.db import Database
from coach.models import Task
from coach.rules import load_rules
from coach.scheduler import CoachScheduler
from tests.coach.conftest import FROZEN, dt

NOW = dt(2026, 10, 15, 9, 0)


def make_bot(tasks):
    coach = FakeCoach(tasks)
    return BotService(coach, FakeMemory(), FakeRouter(), {1}), coach


def test_undated_task_is_scheduled_right_away():
    svc, coach = make_bot([FakeTask("t1", "Remplir le formulaire")])
    replies = svc.on_text("remplir le formulaire")
    assert ("schedule", "t1") in coach.calls
    assert "Planifié" in replies[-1].text
    assert "Modifier la durée" in [b[0] for row in replies[-1].buttons for b in row]


def test_dated_task_still_waits_for_the_button():
    svc, coach = make_bot([FakeTask("t1", "Rendre le devis", due=dt(2026, 10, 20, 18))])
    replies = svc.on_text("rendre le devis avant mardi")
    assert ("schedule", "t1") not in coach.calls
    assert "Planifier" in [b[0] for row in replies[-1].buttons for b in row]


def test_failed_autoplan_keeps_the_card():
    svc, coach = make_bot([FakeTask("t1", "Appeler")])
    coach.schedule = lambda task_id: (_ for _ in ()).throw(RuntimeError("agenda injoignable"))
    replies = svc.on_text("appeler la banque")
    assert "Appeler" in replies[-1].text and replies[-1].buttons


def test_duration_change_on_scheduled_task_reports_new_slot():
    svc, coach = make_bot([FakeTask("t1", "A")])
    coach.extract_tasks("x", "s", "private")
    coach.tasks["t1"].status = "scheduled"
    r = svc.on_callback("durset:t1:90")
    assert "Replanifié" in r.text


def test_duration_change_on_pending_task_just_sets_it():
    svc, coach = make_bot([FakeTask("t1", "A")])
    coach.extract_tasks("x", "s", "private")
    assert "Replanifié" not in svc.on_callback("durset:t1:90").text


def test_store_set_duration_replaces_blocks_and_events():
    db, cal = Database(":memory:"), FakeCalendarClient()
    s = CoachScheduler(db, cal, load_rules(FROZEN), lambda t, b: None, now_fn=lambda: NOW)
    store.configure(db, s)
    t = Task(title="Formulaire", estimated_minutes=30)
    db.upsert_task(t)
    store.schedule(t.id)
    (old,) = db.list_blocks(t.id, open_only=True)
    assert (old.end - old.start).total_seconds() == 30 * 60

    text = store.set_duration(t.id, 90)

    (new,) = db.list_blocks(t.id, open_only=True)
    assert (new.end - new.start).total_seconds() == 90 * 60
    assert new.id != old.id
    (ev,) = cal.events.values()  # l'ancien événement est supprimé : un seul reste, à la nouvelle durée
    assert (ev.end - ev.start).total_seconds() == 90 * 60
    assert db.get_task(t.id).estimated_minutes == 90 and "Planifiée" in text


def test_store_set_duration_without_blocks_returns_none():
    db = Database(":memory:")
    store.configure(db, CoachScheduler(db, FakeCalendarClient(), load_rules(FROZEN), lambda t, b: None,
                                       now_fn=lambda: NOW))
    t = Task(title="X", estimated_minutes=30)
    db.upsert_task(t)
    assert store.set_duration(t.id, 60) is None
    assert db.get_task(t.id).estimated_minutes == 60
