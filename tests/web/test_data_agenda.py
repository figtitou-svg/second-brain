# tests/web/test_data_agenda.py
from __future__ import annotations

from datetime import date, timedelta

from coach.calendar_client import FakeCalendarClient
from coach.db import Database
from coach.models import Block, Event, Project, Task
from coach.rules import load_rules
from coach.scheduler import CoachScheduler
from tests.coach.conftest import FROZEN, dt
from web import data

NOW = dt(2026, 10, 27, 10, 27)   # mardi


def env(busy=None):
    db = Database(":memory:")
    cal = FakeCalendarClient(busy or [])
    sched = CoachScheduler(db, cal, load_rules(FROZEN), lambda t, b=None: None, lambda: NOW)
    return db, sched, data.AgendaCache(cal.agenda)


def test_week_start_is_monday():
    assert data.week_start(date(2026, 10, 27)) == date(2026, 10, 26)
    assert data.week_start(date(2026, 11, 1)) == date(2026, 10, 26)     # dimanche
    assert data.week_start(date(2026, 10, 26)) == date(2026, 10, 26)


def test_agenda_day_empty():
    db, sched, cache = env()
    day = data.agenda_day(db, sched, cache, date(2026, 10, 27))
    assert day.items == [] and day.dues == [] and day.day == date(2026, 10, 27)
    assert day.window_start.hour == 8 and day.window_end.hour == 22


def test_agenda_day_items_kinds_colors_and_actions():
    db, sched, cache = env(busy=[(dt(2026, 10, 27, 8), dt(2026, 10, 27, 10))])
    p = Project(name="Projet IA")
    db.save_project(p)
    t = Task(title="[IA] Entraîner", project_id=p.id, status="scheduled")
    db.upsert_task(t)
    b = Block(task_id=t.id, start=dt(2026, 10, 27, 11), end=dt(2026, 10, 27, 12))
    db.add_block(b)
    db.add_event_if_new(Event(title="Dentiste", start=dt(2026, 10, 27, 17), end=dt(2026, 10, 27, 18)))
    db.set_event_status(db.all_events()[0].id, "added")
    day = data.agenda_day(db, sched, cache, date(2026, 10, 27))
    by_kind = {i.kind: i for i in day.items}
    assert set(by_kind) == {"block", "course", "event"}
    assert by_kind["block"].color == data.project_colors(db)[p.id] and by_kind["block"].block_id == b.id
    assert by_kind["course"].block_id is None and by_kind["course"].color == data.COURSE
    assert all(0 <= i.top <= 100 and 0 < i.height <= 100 and i.top + i.height <= 100.001 for i in day.items)


def test_agenda_day_ignores_other_days_and_missed_blocks():
    db, sched, cache = env()
    t = Task(title="Relire", status="scheduled")
    db.upsert_task(t)
    db.add_block(Block(task_id=t.id, start=dt(2026, 10, 28, 9), end=dt(2026, 10, 28, 10)))          # demain
    db.add_block(Block(task_id=t.id, start=dt(2026, 10, 27, 13), end=dt(2026, 10, 27, 14), outcome="missed"))
    assert data.agenda_day(db, sched, cache, date(2026, 10, 27)).items == []
    assert len(data.agenda_day(db, sched, cache, date(2026, 10, 28)).items) == 1


def test_agenda_clips_to_window():
    db, sched, cache = env()
    t = Task(title="Tôt", status="scheduled")
    db.upsert_task(t)
    db.add_block(Block(task_id=t.id, start=dt(2026, 10, 27, 7, 30), end=dt(2026, 10, 27, 8, 30)))
    (item,) = data.agenda_day(db, sched, cache, date(2026, 10, 27)).items
    assert item.top == 0 and 0 < item.height < 10


def test_overlapping_items_get_separate_lanes():
    db, sched, cache = env(busy=[(dt(2026, 10, 27, 10), dt(2026, 10, 27, 12))])
    t = Task(title="Chevauche", status="scheduled")
    db.upsert_task(t)
    db.add_block(Block(task_id=t.id, start=dt(2026, 10, 27, 11), end=dt(2026, 10, 27, 13)))
    day = data.agenda_day(db, sched, cache, date(2026, 10, 27))
    assert sorted(i.lane for i in day.items) == [0, 1] and all(i.lanes == 2 for i in day.items)


def test_agenda_day_lists_deadlines():
    db, sched, cache = env()
    db.upsert_task(Task(title="[BD] TP", due=dt(2026, 10, 27, 18)))
    db.upsert_task(Task(title="Plus tard", due=dt(2026, 10, 30, 18)))
    db.upsert_task(Task(title="Fait", due=dt(2026, 10, 27, 9), status="done"))
    (due,) = data.agenda_day(db, sched, cache, date(2026, 10, 27)).dues
    assert due.title == "[BD] TP" and due.at == dt(2026, 10, 27, 18)


def test_agenda_week_has_seven_days_from_monday():
    db, sched, cache = env()
    week = data.agenda_week(db, sched, cache, date(2026, 10, 26))
    assert [d.day for d in week] == [date(2026, 10, 26) + timedelta(days=i) for i in range(7)]
    assert week[5].window_start.hour == 10 and week[5].window_end.hour == 20      # samedi : plage du week-end


def test_week_summary_minutes_per_project_and_counts():
    db, sched, cache = env()
    ia, job = Project(name="Projet IA"), Project(name="Job")
    for p in (ia, job):
        db.save_project(p)
    a = Task(title="[IA] A", project_id=ia.id, status="done")
    b = Task(title="[IA] B", project_id=ia.id, status="scheduled")
    c = Task(title="Job C", project_id=job.id, status="done")
    loose = Task(title="Libre", status="done")
    for t in (a, b, c, loose):
        db.upsert_task(t)
    db.add_block(Block(task_id=a.id, start=NOW - timedelta(days=1, hours=2), end=NOW - timedelta(days=1, hours=1), outcome="done"))
    db.add_block(Block(task_id=b.id, start=NOW - timedelta(days=2, hours=2), end=NOW - timedelta(days=2, minutes=30), outcome="done"))
    db.add_block(Block(task_id=c.id, start=NOW - timedelta(days=3, hours=2), end=NOW - timedelta(days=3, hours=1, minutes=30), outcome="done"))
    db.add_block(Block(task_id=loose.id, start=NOW - timedelta(days=4, hours=2), end=NOW - timedelta(days=4, hours=1, minutes=45), outcome="done"))
    db.add_block(Block(task_id=a.id, start=NOW - timedelta(days=9, hours=2), end=NOW - timedelta(days=9, hours=1), outcome="done"))   # hors fenêtre
    s = data.week_summary(db, NOW)
    rows = {r.name: r for r in s.rows}
    assert rows["Projet IA"].minutes == 150 and rows["Job"].minutes == 30 and rows["Sans projet"].minutes == 15
    assert s.rows[0].name == "Projet IA" and s.rows[0].percent == 100 and rows["Job"].percent == 20
    assert s.total_minutes == 195 and s.worked_tasks == 4 and s.finished_tasks == 3
    assert rows["Projet IA"].color == data.project_colors(db)[ia.id] and rows["Sans projet"].color == data.NO_PROJECT


def test_week_summary_late_and_upcoming_and_empty():
    db, sched, cache = env()
    empty = data.week_summary(db, NOW)
    assert empty.rows == [] and empty.total_minutes == 0 and empty.late == [] and empty.upcoming == []
    db.upsert_task(Task(title="En retard", due=NOW - timedelta(days=2)))
    db.upsert_task(Task(title="Cette semaine", due=NOW + timedelta(days=3)))
    db.upsert_task(Task(title="Loin", due=NOW + timedelta(days=20)))
    s = data.week_summary(db, NOW)
    assert [t.title for t in s.late] == ["En retard"] and [t.title for t in s.upcoming] == ["Cette semaine"]
