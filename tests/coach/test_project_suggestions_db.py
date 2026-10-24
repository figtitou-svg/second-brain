from __future__ import annotations

import sqlite3
from datetime import timedelta

import pytest

from coach.db import Database
from coach.models import Suggestion
from tests.coach.conftest import dt

NOW = dt(2026, 10, 21, 10)


def sug(title, batch="b1", project="p1", state="proposed", at=NOW, milestone=None, minutes=30):
    return Suggestion(batch=batch, project_id=project, title=title, state=state, created_at=at,
                      milestone_id=milestone, estimated_minutes=minutes)


def test_roundtrip_and_batch_order():
    db = Database(":memory:")
    a, b, c = sug("Premier", milestone="m1", minutes=90), sug("Deuxième"), sug("Autre lot", batch="b2")
    db.save_suggestions([a, b, c])
    got = db.get_suggestion(a.id)
    assert (got.title, got.milestone_id, got.estimated_minutes, got.state, got.created_at) == (
        "Premier", "m1", 90, "proposed", NOW)
    assert len(a.id) == 8
    assert [s.title for s in db.list_batch("b1")] == ["Premier", "Deuxième"]
    assert db.get_suggestion("zzzzzzzz") is None


def test_state_is_constrained_and_updatable():
    db = Database(":memory:")
    s = sug("X")
    db.save_suggestions([s])
    db.set_suggestion_state(s.id, "planned")
    assert db.get_suggestion(s.id).state == "planned"
    with pytest.raises(sqlite3.IntegrityError):
        db.set_suggestion_state(s.id, "bidon")
    with pytest.raises(ValueError):
        Suggestion(batch="b", project_id="p", title="Y", state="bidon")


def test_project_history_is_newest_first_and_limited():
    db = Database(":memory:")
    db.save_suggestions([sug(f"T{i}") for i in range(6)] + [sug("Autre projet", project="p2")])
    assert [s.title for s in db.list_project_suggestions("p1", 3)] == ["T5", "T4", "T3"]
    assert len(db.list_project_suggestions("p1")) == 6


def test_pending_batch_is_the_newest_with_a_proposed_line():
    db = Database(":memory:")
    assert db.pending_batch("p1") is None
    old = sug("Ancienne", batch="b1", state="added")
    mid = sug("Milieu", batch="b2")
    new = sug("Récente", batch="b3", state="dismissed")
    db.save_suggestions([old, mid, new])
    assert db.pending_batch("p1") == "b2"          # b3 est entièrement traité, b1 aussi
    assert db.pending_batch("autre") is None


def test_dismiss_stale_suggestions():
    db = Database(":memory:")
    old = sug("Vieille", at=NOW - timedelta(days=4))
    recent = sug("Récente", at=NOW - timedelta(days=1))
    done = sug("Déjà ajoutée", at=NOW - timedelta(days=9), state="added")
    db.save_suggestions([old, recent, done])
    assert db.dismiss_stale_suggestions(NOW - timedelta(days=3)) == 1
    assert db.get_suggestion(old.id).state == "dismissed"
    assert db.get_suggestion(recent.id).state == "proposed"
    assert db.get_suggestion(done.id).state == "added"


def test_old_database_gets_the_table(tmp_path):
    path = tmp_path / "old.sqlite"
    c = sqlite3.connect(path)
    c.executescript("CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT NOT NULL, due TEXT, estimated_minutes INTEGER, "
                    "priority TEXT, status TEXT DEFAULT 'pending', source_id TEXT, reschedule_count INTEGER DEFAULT 0, "
                    "created_at TEXT DEFAULT CURRENT_TIMESTAMP);"
                    "INSERT INTO tasks (id,title,priority) VALUES ('t1','vieux','medium');")
    c.commit()
    c.close()
    for i in range(2):
        db = Database(path)
        # 2e ouverture : la suggestion enregistrée à la 1re est toujours là (la migration ne vide rien)
        assert db.pending_batch("p1") == (None if i == 0 else "b1") and db.get_task("t1").title == "vieux"
        db.save_suggestions([sug("X")])
        db.close()


def test_delete_batch_removes_only_the_proposed_lines_of_that_batch():
    db = Database(":memory:")
    a, b, done, other = sug("A"), sug("B"), sug("Faite", state="added"), sug("Autre lot", batch="b2")
    db.save_suggestions([a, b, done, other])
    db.delete_batch("b1")
    assert [s.title for s in db.list_batch("b1")] == ["Faite"]       # une ligne déjà traitée reste dans l'historique
    assert [s.title for s in db.list_batch("b2")] == ["Autre lot"]
    assert db.pending_batch("p1") == "b2"
