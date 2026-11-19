"""Modifier une tâche, placement automatique, question du lundi."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from coach import store
from coach.calendar_client import FakeCalendarClient
from coach.db import Database
from coach.models import Milestone, Project, Task
from coach.rules import load_rules
from coach.scheduler import CoachScheduler
from tests.coach.conftest import FROZEN

TZ = ZoneInfo("Europe/Paris")
NOW = datetime(2026, 11, 23, 9, 0, tzinfo=TZ)        # lundi


def env():
    db = Database(":memory:")
    sent = []
    s = CoachScheduler(db, FakeCalendarClient(), load_rules(FROZEN), lambda t, b=None: sent.append((t, b)),
                       now_fn=lambda: NOW)
    store.configure(db, s)
    return db, s, sent


def test_autoplan_places_open_tasks_only_current_step_and_quietly():
    db, s, sent = env()
    p = Project(name="Security+")
    db.save_project(p)
    d2, d3 = Milestone(project_id=p.id, title="D2", position=1), Milestone(project_id=p.id, title="D3", position=2)
    for m in (d2, d3):
        db.save_milestone(m)
    a = Task(title="[Esp] Fiche", estimated_minutes=60, due=NOW + timedelta(days=3))
    now_step = Task(title="[Sec+] 2.3", project_id=p.id, milestone_id=d2.id, estimated_minutes=60)
    later = Task(title="[Sec+] 3.1", project_id=p.id, milestone_id=d3.id, estimated_minutes=60)
    huge = Task(title="Rapport énorme", estimated_minutes=900)
    for t in (a, now_step, later, huge):
        db.upsert_task(t)
    assert store.autoplan() == 2
    with_slot = {b.task_id for b in db.list_blocks(open_only=True)}
    assert with_slot == {a.id, now_step.id} and not sent           # étape suivante et trop longue : pas placées
    assert store.autoplan() == 0
    assert [t.id for t in store.unsure_tasks()] == [huge.id]
    assert store.monday_question(lambda t, b: sent.append((t, b)))
    assert "Rapport énorme" in sent[-1][0] and sent[-1][1][0][0][1] == f"plan:{huge.id}"
    assert not store.monday_question(lambda t, b: sent.append((t, b)))   # une fois par semaine


def test_edit_task_changes_everything_and_replaces_slots():
    db, s, _ = env()
    p = Project(name="Cyber")
    db.save_project(p)
    m = Milestone(project_id=p.id, title="Attaque", position=0)
    db.save_milestone(m)
    t = Task(title="Vieux titre", estimated_minutes=30, description="ancienne")
    s.schedule_tasks([t])
    old = db.list_blocks(t.id, open_only=True)
    msg = store.edit_task(t.id, {"name": "Nouveau titre", "cat": "cha", "project": p.id, "milestone": m.id,
                                 "date": "2026-11-26", "mode": "on", "minutes": "60", "priority": "high",
                                 "description": ""})
    new = db.get_task(t.id)
    assert "modifiée" in msg and new.title == "[Cha] Nouveau titre" and new.project_id == p.id
    assert new.milestone_id == m.id and new.priority == "high" and new.estimated_minutes == 60
    assert new.not_before.date().isoformat() == "2026-11-26" and new.description is None
    blocks = db.list_blocks(t.id, open_only=True)
    assert blocks and blocks[0].id != old[0].id and blocks[0].start.astimezone(TZ).date().isoformat() == "2026-11-26"
    assert "vide" in store.edit_task(t.id, {"name": "  "})
