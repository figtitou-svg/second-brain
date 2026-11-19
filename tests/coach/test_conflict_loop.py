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


def _env():
    from coach import store
    db = Database(":memory:")
    cal = FakeCalendarClient()
    s = CoachScheduler(db, cal, load_rules(FROZEN), lambda t, b=None: None, now_fn=lambda: NOW)
    store.configure(db, s)
    return db, s, cal


def test_done_task_leaves_google_agenda_and_future_slots_go():
    db, s, cal = _env()
    t = Task(title="[Esp] Fiche", estimated_minutes=240, due=NOW + timedelta(days=5))
    s.schedule_tasks([t])
    blocks = db.list_blocks(t.id, open_only=True)
    assert blocks and all(b.calendar_event_id in cal.events for b in blocks)
    db.set_task_status(t.id, "done")
    assert s.purge_done(NOW) >= len(blocks)
    assert not cal.events and not [b for b in db.list_blocks(t.id, open_only=True) if b.start > NOW]
    assert s.purge_done(NOW) == 0                                  # rien à refaire au passage suivant


def test_remove_bot_event_and_hide_one_occurrence():
    from bot.fakes import FakeMemory, FakeRouter
    from bot.handlers import BotService
    from bot.ports import CoachAdapter
    from coach import agenda_rules
    db, s, cal = _env()
    ev = Event(title="Élections au conseil de département", start=NOW + timedelta(days=1),
               end=NOW + timedelta(days=1, hours=6))
    db.add_event_if_new(ev)
    db.set_event_status(ev.id, "added")
    svc = BotService(CoachAdapter(), FakeMemory(), FakeRouter("x"), 1)
    assert "retiré" in svc.on_callback(f"evdel:{ev.id}").text and db.list_events("added") == []
    start = NOW + timedelta(days=1, hours=2)
    key = agenda_rules.occurrence_key("BDR · TD", start)
    try:
        r = svc.on_callback(f"hideo:{key}")
        assert r.buttons == [[("↩ Annuler", f"unhideo:{key}")]]
        assert agenda_rules.is_hidden("BDR · TD", start) and not agenda_rules.is_hidden("BDR · TD", start + timedelta(days=7))
        svc.on_callback(f"unhideo:{key}")
        assert not agenda_rules.is_hidden("BDR · TD", start)
    finally:
        agenda_rules.set_hidden_source(None)


def test_deleting_an_event_already_gone_from_google_is_not_an_error():
    from coach.calendar_client import GoogleCalendarClient

    class Gone(Exception):
        def __init__(self, status):
            super().__init__(f"HttpError {status}")
            self.resp = type("R", (), {"status": status})()

    class Svc:
        def __init__(self, status):
            self.status = status

        def events(self):
            return self

        def delete(self, **k):
            return self

        def execute(self):
            raise Gone(self.status)
    GoogleCalendarClient(Svc(410), "primary").delete_event("x")      # « Resource has been deleted »
    GoogleCalendarClient(Svc(404), "primary").delete_event("x")
    import pytest
    with pytest.raises(Gone):
        GoogleCalendarClient(Svc(500), "primary").delete_event("x")  # une vraie panne remonte toujours
