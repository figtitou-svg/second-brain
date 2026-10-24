from datetime import timedelta

import notion_sync
from coach import store
from coach.calendar_client import FakeCalendarClient
from coach.db import Database
from coach.models import Task
from coach.rules import load_rules
from coach.scheduler import CoachScheduler
from notion_sync import Row
from tests.coach.conftest import FROZEN, dt

NOW = dt(2026, 10, 18, 18, 30)  # dimanche


class FakeNotion:
    def __init__(self, rows):
        self.rows, self.done = rows, []

    def fetch_all(self): return self.rows
    def set_done(self, page_id): self.done.append(page_id)


def env():
    db, sent = Database(":memory:"), []
    s = CoachScheduler(db, FakeCalendarClient(), load_rules(FROZEN), lambda t, b: sent.append(t), now_fn=lambda: NOW)
    store.configure(db, s)
    return db, s, sent


def run_sync(db, client, sent, s=None):
    s = s or store._sched
    return notion_sync.sync(db, client, lambda t, b: sent.append(t), lambda tid: store.mark_done(tid), lambda i: [], s)


ROWS = [Row("aaaa-1", "Terminer TP3", "Programmation en langage C", "2026-10-20", "À faire"),
        Row("bbbb-2", "5 mails sponso", None, None, "À faire"),
        Row("cccc-3", "Vieux truc", "EPS", "2026-10-01", "terminé")]


def test_import_summary_once_then_idempotent():
    db, s, sent = env()
    st = run_sync(db, FakeNotion(ROWS), sent)
    assert st["new"] == 1 and len(sent) == 1 and "importées" in sent[0]
    (t,) = db.list_tasks()
    assert t.title == "[C] Terminer TP3" and t.due.date().isoformat() == "2026-10-20"
    run_sync(db, FakeNotion(ROWS), sent)
    assert len(sent) == 1 and len(db.list_tasks()) == 1


def test_notion_done_closes_task_and_telegram_done_pushes_back():
    db, s, sent = env()
    run_sync(db, FakeNotion(ROWS), sent)
    tid = db.list_tasks()[0].id
    closed = [ROWS[0].__class__("aaaa-1", "Terminer TP3", "Programmation en langage C", "2026-10-20", "terminé")]
    run_sync(db, FakeNotion(closed), sent)
    assert db.get_task(tid).status == "done"
    # sens inverse
    db2, s2, sent2 = env()
    c = FakeNotion(ROWS)
    run_sync(db2, c, sent2)
    store.mark_done(db2.list_tasks()[0].id)
    assert run_sync(db2, c, sent2)["pushed"] == 1 and c.done == ["aaaa-1"]


def test_weekly_recap_sunday_once_and_content():
    db, s, sent = env()
    db.upsert_task(Task(id="a", title="Rendre TP", due=NOW + timedelta(days=2), estimated_minutes=90))
    db.upsert_task(Task(id="b", title="En retard", due=NOW - timedelta(days=1)))
    db.upsert_task(Task(id="c", title="Projet lointain", due=NOW + timedelta(days=30)))
    s.tick(NOW)
    s.tick(NOW + timedelta(minutes=5))
    recaps = [m for m in sent if "Récap de la semaine" in m]
    assert len(recaps) == 1
    r = recaps[0]
    assert "En retard" in r and "Rendre TP" in r and "Plus tard" in r and "Projet lointain" in r


def test_marker_all_day_transparent_and_block_1h_planned_colored():
    db, s, sent = env()
    run_sync(db, FakeNotion(ROWS), sent)
    (t,) = db.list_tasks()
    cal = s.calendar
    (mk,) = cal.markers.values()
    assert mk["start"] == {"date": "2026-10-20"} and mk["end"] == {"date": "2026-10-21"}
    assert mk["transparency"] == "transparent" and mk["colorId"] == "11"  # [C]
    (b,) = db.list_blocks(t.id)
    assert (b.end - b.start) == timedelta(hours=1) and b.start < t.due
    (ev,) = cal.events.values()
    assert ev.summary.startswith("🎯")
    # idempotent : ni second marqueur, ni second bloc
    run_sync(db, FakeNotion(ROWS), sent)
    assert len(cal.markers) == 1 and len(db.list_blocks(t.id)) == 1


def test_marker_moves_with_date_and_disappears_when_done():
    db, s, sent = env()
    run_sync(db, FakeNotion(ROWS), sent)
    moved = [Row("aaaa-1", "Terminer TP3", "Programmation en langage C", "2026-10-25", "À faire")]
    run_sync(db, FakeNotion(moved), sent)
    (mk,) = s.calendar.markers.values()
    assert mk["start"] == {"date": "2026-10-25"}
    run_sync(db, FakeNotion([Row("aaaa-1", "Terminer TP3", "Programmation en langage C", "2026-10-25", "terminé")]), sent)
    assert s.calendar.markers == {}


def test_overdue_notion_task_replanned_asap():
    db, s, sent = env()
    late = [Row("dddd-4", "Mail agence", "EPS", "2026-10-12", "À faire")]
    run_sync(db, FakeNotion(late), sent)
    (t,) = db.list_tasks()
    assert t.due.date().isoformat() == "2026-10-12"  # vraie date conservée
    assert len(db.list_blocks(t.id)) == 1


def test_daily_cap_counts_blocks_placed_by_earlier_calls():
    db, s, sent = env()
    rows = [Row(f"p{i}-x", f"Tache {i}", "EPS", "2026-10-30", "À faire") for i in range(6)]
    run_sync(db, FakeNotion(rows), sent)
    per_day: dict = {}
    for b in db.list_blocks():
        per_day[b.start.date()] = per_day.get(b.start.date(), 0) + b.minutes
    cap = load_rules(FROZEN).max_focus_minutes_per_day
    assert per_day and max(per_day.values()) <= cap


def test_replanning_a_task_never_leaves_two_open_blocks():
    db, s, sent = env()
    run_sync(db, FakeNotion(ROWS), sent)
    (t,) = [x for x in db.list_tasks() if x.title.endswith("TP3")]
    assert len(db.list_blocks(t.id, open_only=True)) == 1
    db.set_task_status(t.id, "pending")  # la tâche redevient à planifier (ex. : la date change)
    moved = [Row("aaaa-1", "Terminer TP3", "Programmation en langage C", "2026-10-24", "À faire")]
    run_sync(db, FakeNotion(moved), sent)
    assert len(db.list_blocks(t.id, open_only=True)) == 1
    assert len(s.calendar.events) == 1


def test_notion_row_absorbs_task_already_known_from_a_mail():
    db, s, sent = env()
    mail = Task(title="Terminer le TP3", estimated_minutes=90, due=dt(2026, 10, 21, 18), source_id="gmail:m1")
    s.schedule_tasks([mail])
    run_sync(db, FakeNotion(ROWS[:1]), sent)
    nt = db.get_task(notion_sync.task_id("aaaa-1"))
    assert db.get_task(mail.id).status == "dropped" and not db.list_blocks(mail.id, open_only=True)
    assert nt.status == "scheduled" and nt.estimated_minutes == 90  # la durée choisie est gardée
    open_tasks = db.list_tasks("pending", "scheduled")
    assert [t.id for t in open_tasks] == [nt.id]  # une seule tâche, un seul bloc dans l'agenda
    assert len(s.calendar.events) == 1


def test_notion_row_does_not_reopen_a_finished_task_twice():
    db, s, sent = env()
    done = Task(title="Terminer le TP3", due=dt(2026, 10, 21, 18), source_id="gmail:m1", status="done")
    db.upsert_task(done)
    run_sync(db, FakeNotion(ROWS[:1]), sent)
    assert db.get_task(done.id).status == "done"  # Notion reste la source : la ligne « À faire » est suivie
    assert db.get_task(notion_sync.task_id("aaaa-1")) is not None
