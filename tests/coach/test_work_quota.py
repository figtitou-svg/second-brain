"""10 h de job par semaine pour Franck : une tâche par semaine, prioritaire, en blocs d'1 h à 1 h 30, midi permis."""
from datetime import date, datetime, time, timedelta

from coach import store, work_quota as wq
from coach.calendar_client import FakeCalendarClient
from coach.db import Database
from coach.models import Task
from coach.planner import plan_detailed
from coach.rules import load_rules
from coach.scheduler import CoachScheduler
from tests.coach.conftest import FROZEN

TZ = wq.TZ
NOW = datetime(2026, 11, 17, 9, 0, tzinfo=TZ)        # mardi


def test_week_tasks_period_and_ids():
    got = wq.due_tasks(set(), NOW)
    assert [t.id for t in got] == ["quota-franck-2026-11-16", "quota-franck-2026-11-23"]
    this = got[0]
    assert this.estimated_minutes == 600 and this.priority == "high" and this.not_before == NOW
    assert this.due == datetime(2026, 11, 22, 23, 59, 59, tzinfo=TZ)
    assert wq.due_tasks({t.id for t in got}, NOW) == []                      # jamais recréée
    assert wq.week_task(wq.QUOTAS[0], date(2027, 7, 5), NOW) is None          # après le 30 juin
    last = wq.week_task(wq.QUOTAS[0], date(2027, 6, 28), NOW)
    assert last.due.date() == date(2027, 6, 30)


def test_planner_blocks_of_one_hour_to_ninety_minutes_lunch_allowed():
    rules = load_rules(FROZEN)
    lunch = [b for b in rules.blocked if b.label.lower() == "repas"]
    task = wq.week_task(wq.QUOTAS[0], date(2026, 11, 16), NOW)
    other = Task(title="Réviser", estimated_minutes=120, priority="high",
                 due=datetime(2026, 11, 22, 23, 0, tzinfo=TZ))
    res = plan_detailed([other, task], [], rules, NOW)
    mine = [b for b in res.blocks if b.task_id == task.id]
    assert sum(b.minutes for b in mine) == 600 and not res.unplaced
    assert {b.minutes for b in mine} <= {60, 90}
    if lunch:   # avec un repas bloqué dans les règles de test, au moins un bloc Franck peut le chevaucher
        assert all(wq.skips_blocked(task, b.label) for b in lunch)
    assert not wq.skips_blocked(other, "Repas")


def test_store_creates_and_places_once():
    db = Database(":memory:")
    sched = CoachScheduler(db, FakeCalendarClient(), load_rules(FROZEN), lambda t, b=None: None, now_fn=lambda: NOW)
    store.configure(db, sched)
    assert store.ensure_work_quota() == 2
    assert store.ensure_work_quota() == 0
    blocks = [b for b in db.list_blocks(open_only=True) if b.task_id == "quota-franck-2026-11-16"]
    assert sum(b.minutes for b in blocks) == 600
    assert all(b.start >= NOW and b.end <= datetime(2026, 11, 23, tzinfo=TZ) for b in blocks)


def test_at_most_three_hours_a_day():
    task = wq.week_task(wq.QUOTAS[0], date(2026, 11, 16), datetime(2026, 11, 20, 7, 0, tzinfo=TZ))  # vendredi
    res = plan_detailed([task], [], load_rules(FROZEN), datetime(2026, 11, 20, 7, 0, tzinfo=TZ))
    per_day = {}
    for b in res.blocks:
        per_day[b.start.date()] = per_day.get(b.start.date(), 0) + b.minutes
    assert per_day and max(per_day.values()) <= 180
    assert sum(per_day.values()) + sum(res.unplaced.values()) == 600   # vendredi -> dimanche : 9 h au plus
