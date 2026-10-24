from __future__ import annotations

import pytest

from coach.calendar_client import FakeCalendarClient
from coach.db import Database
from coach.models import Task
from coach.rules import load_rules
from coach.scheduler import CoachScheduler
from tests.coach.conftest import FROZEN, dt

NOW = dt(2026, 10, 15, 9, 0)  # jeudi


@pytest.fixture
def env():
    db = Database(":memory:")
    cal = FakeCalendarClient([(dt(2026, 10, 15, 18, 30), dt(2026, 10, 15, 21, 30))])
    s = CoachScheduler(db, cal, load_rules(FROZEN), lambda text, buttons=None: None, lambda: NOW)
    return s, db, cal


def mk(title, minutes=30, due=None):
    return Task(title=title, estimated_minutes=minutes, due=due or dt(2026, 10, 20, 18))


def event_of(db, cal, task):
    return cal.events[db.list_blocks(task.id)[0].calendar_event_id]


def test_new_block_of_a_focus_task_is_orange_with_a_star(env):
    s, db, cal = env
    t = mk("Urgent", due=dt(2026, 10, 16, 18))
    s.schedule_tasks([t])
    ev = event_of(db, cal, t)
    assert ev.summary == "🎯 ⭐ Urgent" and ev.color == "6"
    assert db.list_blocks(t.id)[0].style == "focus"


def test_blocks_outside_the_focus_use_the_blue_scale(env):
    s, db, cal = env
    urgent = [mk(f"u{i}", due=dt(2026, 10, 15, 20)) for i in range(3)]
    mid, far = mk("mid", due=dt(2026, 10, 18, 18)), mk("far", due=dt(2026, 11, 20, 18))
    s.schedule_tasks(urgent + [mid, far])
    for t in urgent:
        assert event_of(db, cal, t).color == "6"
    assert (event_of(db, cal, mid).color, event_of(db, cal, mid).summary) == ("7", "🎯 mid")
    assert (event_of(db, cal, far).color, event_of(db, cal, far).summary) == ("1", "🎯 far")


def test_up_moves_the_star_to_the_new_focus_task(env):
    s, db, cal = env
    tasks = [mk(n, due=dt(2026, 10, 20, 18)) for n in "ABCD"]
    s.schedule_tasks(tasks)
    assert [event_of(db, cal, t).summary for t in tasks] == ["🎯 ⭐ A", "🎯 ⭐ B", "🎯 ⭐ C", "🎯 D"]
    s.adjust_importance(tasks[3].id, True)
    s.adjust_importance(tasks[3].id, True)
    assert event_of(db, cal, tasks[3]).summary == "🎯 ⭐ D" and event_of(db, cal, tasks[3]).color == "6"
    assert event_of(db, cal, tasks[2]).summary == "🎯 C"  # C a quitté le focus


def test_sync_patches_only_what_changed(env):
    s, db, cal = env
    tasks = [mk(n, due=dt(2026, 10, 20, 18)) for n in "ABCD"]
    s.schedule_tasks(tasks)
    calls = []
    real = cal.recolor_event
    cal.recolor_event = lambda *a: (calls.append(a), real(*a))[1]
    s.sync_styles()
    assert calls == []  # tout est déjà au bon style
    db.set_task_boost(tasks[3].id, 2)
    s.sync_styles()
    assert len(calls) == 2  # D gagne l'étoile, C la perd
    s.sync_styles()
    assert len(calls) == 2


def test_recolor_failure_is_retried_next_time(env):
    s, db, cal = env
    tasks = [mk(n, due=dt(2026, 10, 20, 18)) for n in "ABCD"]
    s.schedule_tasks(tasks)
    real = cal.recolor_event

    def boom(*a):
        raise RuntimeError("API agenda indisponible")

    cal.recolor_event = boom
    db.set_task_boost(tasks[3].id, 2)
    s.sync_styles()  # ne lève pas
    assert db.list_blocks(tasks[3].id)[0].style == "moyen"  # style non mémorisé
    cal.recolor_event = real
    s.sync_styles()
    assert db.list_blocks(tasks[3].id)[0].style == "focus"
    assert event_of(db, cal, tasks[3]).summary == "🎯 ⭐ D"


def test_tick_resyncs_styles(env):
    s, db, cal = env
    tasks = [mk(n, due=dt(2026, 10, 20, 18)) for n in "ABCD"]
    s.schedule_tasks(tasks)
    db.set_task_boost(tasks[3].id, 2)
    s.tick(NOW)
    assert event_of(db, cal, tasks[3]).summary == "🎯 ⭐ D"
