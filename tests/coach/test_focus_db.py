from __future__ import annotations

import sqlite3

import pytest

from coach.db import Database
from coach.models import Block, Task
from tests.coach.conftest import dt


def test_boost_style_and_weights_roundtrip():
    db = Database(":memory:")
    t = Task(title="[Alg] TP", boost=2)
    db.upsert_task(t)
    assert db.get_task(t.id).boost == 2
    db.set_task_boost(t.id, -1)
    assert db.get_task(t.id).boost == -1

    b = Block(task_id=t.id, start=dt(2026, 10, 15, 9), end=dt(2026, 10, 15, 10), style="focus")
    db.add_block(b)
    assert db.get_block(b.id).style == "focus"
    db.set_block_style(b.id, "haut")
    assert db.get_block(b.id).style == "haut"

    assert db.get_weights() == {}
    db.set_weight("Alg", 1.1)
    db.set_weight("Alg", 1.21)  # remplace
    assert db.get_weights() == {"Alg": 1.21}


def test_boost_is_bounded_by_the_model():
    with pytest.raises(ValueError):
        Task(title="x", boost=3)
    with pytest.raises(ValueError):
        Task(title="x", boost=-2)


def test_done_minutes_sums_only_done_blocks():
    db = Database(":memory:")
    t = Task(title="x")
    db.upsert_task(t)
    db.add_block(Block(task_id=t.id, start=dt(2026, 10, 15, 9), end=dt(2026, 10, 15, 10), outcome="done"))
    db.add_block(Block(task_id=t.id, start=dt(2026, 10, 15, 11), end=dt(2026, 10, 15, 11, 30), outcome="done"))
    db.add_block(Block(task_id=t.id, start=dt(2026, 10, 15, 14), end=dt(2026, 10, 15, 15)))  # ouvert
    assert db.done_minutes() == {t.id: 90}


def test_old_database_is_migrated(tmp_path):
    path = tmp_path / "old.sqlite"
    c = sqlite3.connect(path)
    c.executescript(
        "CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT NOT NULL, due TEXT, estimated_minutes INTEGER, "
        "priority TEXT, status TEXT DEFAULT 'pending', source_id TEXT, reschedule_count INTEGER DEFAULT 0, "
        "created_at TEXT DEFAULT CURRENT_TIMESTAMP);"
        "CREATE TABLE blocks (id TEXT PRIMARY KEY, task_id TEXT, calendar_event_id TEXT, "
        "start TEXT NOT NULL, end TEXT NOT NULL, outcome TEXT);"
        "INSERT INTO tasks (id,title,priority) VALUES ('t1','vieux','medium');"
        "INSERT INTO blocks (id,task_id,start,end) VALUES ('b1','t1','2026-10-15T09:00:00+02:00','2026-10-15T10:00:00+02:00');"
    )
    c.commit()
    c.close()
    for _ in range(2):  # la seconde ouverture ne doit pas échouer sur « colonne déjà présente »
        db = Database(path)
        t = db.get_task("t1")
        assert t.boost == 0 and t.not_before is None
        assert db.get_block("b1").style is None
        assert db.get_weights() == {}
        db.close()
