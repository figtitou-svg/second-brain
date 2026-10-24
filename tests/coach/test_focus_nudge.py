from __future__ import annotations

import pytest

from coach.calendar_client import FakeCalendarClient
from coach.db import Database
from coach.models import Block, Task
from coach.rules import load_rules
from coach.scheduler import CoachScheduler
from tests.coach.conftest import FROZEN, dt

NOW = dt(2026, 10, 15, 9, 0)  # jeudi


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def env():
    db = Database(":memory:")
    cal = FakeCalendarClient()
    sent: list[tuple[str, list | None]] = []
    clock = Clock(NOW)
    s = CoachScheduler(db, cal, load_rules(FROZEN), lambda text, buttons=None: sent.append((text, buttons)), clock)
    return s, db, cal, sent, clock


def setup(env, hour=17):
    """3 rendus urgents (le focus) et 2 h de petites tâches terminées ce matin, hors focus."""
    s, db, cal, sent, clock = env
    clock.t = dt(2026, 10, 15, hour, 0)
    urgent = [Task(title=f"Rendu {n}", estimated_minutes=60, due=dt(2026, 10, 16, 18)) for n in "ABC"]
    for t in urgent:
        db.upsert_task(t)
    for i, (h1, m1, h2, m2) in enumerate([(9, 0, 10, 0), (10, 30, 11, 30)]):
        small = Task(title=f"Petite {i}", estimated_minutes=60, due=dt(2026, 10, 30, 18), status="done")
        db.upsert_task(small)
        db.add_block(Block(task_id=small.id, start=dt(2026, 10, 15, h1, m1), end=dt(2026, 10, 15, h2, m2),
                           outcome="done"))
    return urgent


def nudges(sent):
    return [(m, b) for m, b in sent if "pas commencé" in m]


def test_nominal_nudge_with_buttons(env):
    s, db, cal, sent, clock = env
    a, *_ = setup(env)
    s.tick(clock.t)
    (text, buttons), = nudges(sent)
    assert "Rendu A" in text and "2 h" in text
    assert [lbl for lbl, _ in buttons[0]] == ["Je m'y mets", "Pas maintenant"]
    assert buttons[0][0][1] == f"nudgego:{a.id}" and buttons[0][1][1] == f"nudgeno:{a.id}"


def test_at_most_two_per_day_and_one_per_task(env):
    s, db, cal, sent, clock = env
    setup(env)
    for _ in range(4):
        s.tick(clock.t)
    msgs = nudges(sent)
    assert len(msgs) == 2 and "Rendu A" in msgs[0][0] and "Rendu B" in msgs[1][0]


def test_no_nudge_with_less_than_90_minutes_off_focus(env):
    s, db, cal, sent, clock = env
    setup(env)
    small = db.list_tasks("done")[1]  # le matin n'a plus qu'un bloc de 60 min : on retire le second
    db.delete_block(db.list_blocks(small.id)[0].id)
    s.tick(clock.t)
    assert nudges(sent) == []


def test_no_nudge_outside_allowed_hours(env):
    s, db, cal, sent, clock = env
    setup(env, hour=23)
    s.tick(clock.t)
    assert nudges(sent) == []


def test_no_nudge_for_a_task_that_is_not_urgent(env):
    s, db, cal, sent, clock = env
    for n in "ABC":
        db.upsert_task(Task(title=f"Lointain {n}", estimated_minutes=60, due=dt(2026, 11, 20, 18)))
    clock.t = dt(2026, 10, 15, 17, 0)
    for i, (h1, h2) in enumerate([(9, 10), (10, 11)]):
        small = Task(title=f"Petite {i}", estimated_minutes=60, due=dt(2026, 10, 30, 18), status="done")
        db.upsert_task(small)
        db.add_block(Block(task_id=small.id, start=dt(2026, 10, 15, h1), end=dt(2026, 10, 15, h2), outcome="done"))
    s.tick(clock.t)
    assert nudges(sent) == []


def test_no_nudge_close_to_a_block_edge(env):
    s, db, cal, sent, clock = env
    a, b, c = setup(env)
    db.add_block(Block(task_id=c.id, start=dt(2026, 10, 15, 17, 10), end=dt(2026, 10, 15, 18, 0)))
    s.tick(clock.t)
    assert nudges(sent) == []


def test_task_with_a_block_starting_soon_is_skipped(env):
    s, db, cal, sent, clock = env
    a, b, c = setup(env)
    db.add_block(Block(task_id=a.id, start=dt(2026, 10, 15, 18, 0), end=dt(2026, 10, 15, 19, 0)))
    s.tick(clock.t)
    (text, _), = nudges(sent)
    assert "Rendu B" in text


def test_task_with_a_running_block_is_skipped(env):
    s, db, cal, sent, clock = env
    a, b, c = setup(env)
    db.add_block(Block(task_id=a.id, start=dt(2026, 10, 15, 16, 30), end=dt(2026, 10, 15, 18, 0)))
    s.tick(clock.t)
    (text, _), = nudges(sent)
    assert "Rendu B" in text and "Rendu A" not in text


def test_task_already_worked_on_today_is_skipped(env):
    s, db, cal, sent, clock = env
    a, b, c = setup(env)
    db.add_block(Block(task_id=a.id, start=dt(2026, 10, 15, 11, 40), end=dt(2026, 10, 15, 12, 10), outcome="done"))
    s.tick(clock.t)
    (text, _), = nudges(sent)
    assert "Rendu B" in text


def test_start_now_places_a_block_right_away(env):
    s, db, cal, sent, clock = env
    a, *_ = setup(env)
    msg = s.start_now(a.id)
    blocks = db.list_blocks(a.id, open_only=True)
    assert len(blocks) == 1 and dt(2026, 10, 15, 17, 0) <= blocks[0].start <= dt(2026, 10, 15, 17, 15)
    assert "Rendu A" in msg and len(cal.events) == 1


def test_start_now_on_a_finished_task(env):
    s, db, cal, sent, clock = env
    t = Task(title="Fini", status="done")
    db.upsert_task(t)
    assert "n'est plus ouverte" in s.start_now(t.id)
