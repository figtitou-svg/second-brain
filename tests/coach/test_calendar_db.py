from __future__ import annotations

from datetime import datetime

from coach.calendar_client import (CalendarClient, FakeCalendarClient, GoogleCalendarClient,
                                   TASK_ID_RE)
from coach.db import Database
from coach.models import Block, Task
from tests.coach.conftest import dt


class _Exec:
    def __init__(self, result, sink, name, kwargs):
        self.result, self.sink = result, sink
        sink.append((name, kwargs))

    def execute(self):
        return self.result


class FakeService:
    """Imite googleapiclient.discovery.Resource, sans réseau."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def freebusy(self):
        outer = self

        class FB:
            def query(self, body):
                return _Exec({"calendars": {"primary": {"busy": [
                    {"start": "2026-10-15T07:00:00Z", "end": "2026-10-15T09:00:00Z"}]}}},
                    outer.calls, "freebusy", body)
        return FB()

    def events(self):
        outer = self

        class EV:
            def insert(self, calendarId, body):
                return _Exec({"id": "ev1"}, outer.calls, "insert", body)

            def patch(self, calendarId, eventId, body):
                return _Exec({}, outer.calls, "patch", {"id": eventId, **body})

            def delete(self, calendarId, eventId):
                return _Exec(None, outer.calls, "delete", {"id": eventId})
        return EV()


def _task_block():
    t = Task(title="Rapport", estimated_minutes=60)
    b = Block(task_id=t.id, start=dt(2026, 10, 15, 14), end=dt(2026, 10, 15, 15))
    return t, b


def test_google_create_event():
    svc = FakeService()
    g = GoogleCalendarClient(svc)
    t, b = _task_block()
    assert g.create_event(b, t) == "ev1"
    body = svc.calls[0][1]
    assert body["summary"] == "🎯 Rapport"
    assert TASK_ID_RE.search(body["description"]).group(1) == t.id
    assert body["start"]["timeZone"] == "Europe/Paris"
    assert body["start"]["dateTime"].endswith("+02:00")


def test_google_freebusy_converted_to_paris():
    g = GoogleCalendarClient(FakeService())
    busy = g.freebusy(dt(2026, 10, 15), dt(2026, 10, 16))
    assert busy == [(dt(2026, 10, 15, 9), dt(2026, 10, 15, 11))]
    assert busy[0][0].utcoffset().total_seconds() == 7200


def test_google_move_and_delete():
    svc = FakeService()
    g = GoogleCalendarClient(svc)
    g.move_event("ev1", dt(2026, 10, 16, 9), dt(2026, 10, 16, 10))
    g.delete_event("ev1")
    assert svc.calls[0][0] == "patch" and svc.calls[0][1]["id"] == "ev1"
    assert svc.calls[1][0] == "delete"


def test_fake_calendar_roundtrip_and_protocol():
    f = FakeCalendarClient([(dt(2026, 10, 15, 8), dt(2026, 10, 15, 10))])
    assert isinstance(f, CalendarClient) and isinstance(GoogleCalendarClient(FakeService()), CalendarClient)
    t, b = _task_block()
    eid = f.create_event(b, t)
    assert f.events[eid].summary == "🎯 Rapport"
    assert (b.start, b.end) in f.freebusy(dt(2026, 10, 15), dt(2026, 10, 16))
    f.move_event(eid, dt(2026, 10, 16, 9), dt(2026, 10, 16, 10))
    assert f.events[eid].start == dt(2026, 10, 16, 9)
    f.delete_event(eid)
    assert eid not in f.events


def test_db_roundtrip():
    db = Database(":memory:")
    t = Task(title="A", due=dt(2026, 10, 20, 18), estimated_minutes=45, priority="high", source_id="s")
    db.upsert_task(t)
    assert db.get_task(t.id) == t
    b = Block(task_id=t.id, start=dt(2026, 10, 15, 9), end=dt(2026, 10, 15, 10), calendar_event_id="e")
    db.add_block(b)
    assert db.get_block(b.id) == b
    db.set_block_outcome(b.id, "done")
    assert db.list_blocks(t.id, open_only=True) == []
    assert db.increment_reschedule(t.id) == 1
    db.set_task_status(t.id, "scheduled")
    assert [x.id for x in db.list_tasks("scheduled")] == [t.id]
    t2 = t.model_copy(update={"title": "B"})
    db.upsert_task(t2)
    assert db.get_task(t.id).title == "B"


def test_db_notification_claim_once():
    db = Database(":memory:")
    assert db.claim_notification("k") is True
    assert db.claim_notification("k") is False


def test_db_file_created(tmp_path):
    p = tmp_path / "data" / "coach.sqlite"
    Database(p).close()
    assert p.exists()


def test_google_freebusy_long_range_is_chunked():
    """Google refuse > ~3 mois (timeRangeTooLong) : on découpe en tranches de 60 jours."""
    from coach.calendar_client import FREEBUSY_CHUNK_DAYS
    svc = FakeService()
    g = GoogleCalendarClient(svc)
    g.freebusy(dt(2026, 10, 15), dt(2027, 3, 15))  # ~150 jours
    queries = [c for c in svc.calls if c[0] == "freebusy"]
    assert len(queries) == 3
    for _, body in queries:
        lo = datetime.fromisoformat(body["timeMin"])
        hi = datetime.fromisoformat(body["timeMax"])
        assert (hi - lo).days <= FREEBUSY_CHUNK_DAYS


def test_not_before_roundtrip():
    from coach.db import Database
    from coach.models import Task
    from tests.coach.conftest import dt
    db = Database(":memory:")
    t = Task(title="Chaussures", due=dt(2026, 10, 16, 23, 59), not_before=dt(2026, 10, 16))
    db.upsert_task(t)
    assert db.get_task(t.id).not_before == dt(2026, 10, 16)


def test_card_says_on_day_not_before_day():
    from coach import fmt
    from coach.models import Task
    from tests.coach.conftest import dt
    on = Task(title="Chaussures", due=dt(2026, 10, 16, 23, 59), not_before=dt(2026, 10, 16))
    assert "📆 le ven. 16 oct." in fmt.task_card(on) and "avant" not in fmt.task_card(on)
    before = Task(title="Rapport", due=dt(2026, 10, 16, 23, 59))
    assert "avant" in fmt.task_card(before)


def test_google_recolor_event_patches_title_and_color():
    svc = FakeService()
    g = GoogleCalendarClient(svc)
    g.recolor_event("ev1", "🎯 ⭐ Rapport", "6")
    assert svc.calls[0] == ("patch", {"id": "ev1", "summary": "🎯 ⭐ Rapport", "colorId": "6"})


def test_event_title_and_level_colors():
    from coach.calendar_client import STYLE_COLOR, event_title
    t = Task(title="Rapport")
    assert event_title(t) == "🎯 Rapport" and event_title(t, "haut") == "🎯 Rapport"
    assert event_title(t, "focus") == "🎯 ⭐ Rapport"
    assert STYLE_COLOR == {"focus": "6", "haut": "9", "moyen": "7", "faible": "1"}


def test_google_create_event_with_level_sets_color_and_star():
    svc = FakeService()
    g = GoogleCalendarClient(svc)
    t, b = _task_block()
    g.create_event(b, t, "focus")
    body = svc.calls[0][1]
    assert body["summary"] == "🎯 ⭐ Rapport" and body["colorId"] == "6"
