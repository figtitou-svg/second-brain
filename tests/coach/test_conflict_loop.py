"""Boucle du 18/11 : un rendez-vous ajouté par le bot mais absent de Google ne doit pas faire déplacer le même
bloc toutes les 5 minutes."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from coach.calendar_client import FakeCalendarClient
from coach.db import Database
from coach.models import Event, Task
from coach.rules import load_rules
from coach.scheduler import CoachScheduler
from tests.coach.conftest import FROZEN

TZ = ZoneInfo("Europe/Paris")
NOW = datetime(2026, 11, 18, 8, 0, tzinfo=TZ)


def test_moved_block_never_lands_back_on_the_event():
    db = Database(":memory:")
    s = CoachScheduler(db, FakeCalendarClient(), load_rules(FROZEN), lambda t, b=None: None, now_fn=lambda: NOW)
    t = Task(title="[Esp] Ejervicio", estimated_minutes=60, due=NOW + timedelta(days=5))
    s.schedule_tasks([t])
    blk = db.list_blocks(t.id, open_only=True)[0]
    ev = Event(title="Campus du Libre", start=blk.start - timedelta(hours=1), end=blk.end + timedelta(hours=3))
    db.add_event_if_new(ev)
    db.set_event_status(ev.id, "added")                         # ajouté, mais jamais écrit dans Google
    first = s.resolve_conflicts(NOW - timedelta(minutes=1))
    assert len(first) == 1 and "passe avant" in first[0]
    new = db.list_blocks(t.id, open_only=True)[0]
    assert not (new.start < ev.end and new.end > ev.start)    # le nouveau créneau évite le rendez-vous
    assert s.resolve_conflicts(NOW - timedelta(minutes=1)) == []                      # et rien ne bouge au passage suivant
