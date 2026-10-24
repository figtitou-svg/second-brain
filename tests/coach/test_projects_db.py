from __future__ import annotations

import sqlite3
from datetime import date

import pytest

from coach.db import Database
from coach.models import Milestone, Project, Task
from tests.coach.conftest import dt


def test_project_roundtrip_and_listing():
    db = Database(":memory:")
    p = Project(name="Projet IA", category="IA", deadline=date(2026, 12, 15), profile={"goal": "entraîner un modèle"})
    db.save_project(p)
    got = db.get_project(p.id)
    assert (got.name, got.category, got.status, got.deadline) == ("Projet IA", "IA", "active", date(2026, 12, 15))
    assert got.profile == {"goal": "entraîner un modèle"} and len(p.id) == 8
    db.save_project(got.model_copy(update={"status": "paused"}))
    assert [x.id for x in db.list_projects("paused")] == [p.id]
    assert db.list_projects("active") == [] and len(db.list_projects()) == 1


def test_category_is_unique_among_unfinished_projects_and_freed_when_done():
    db = Database(":memory:")
    a = Project(name="A", category="IA")
    db.save_project(a)
    with pytest.raises(sqlite3.IntegrityError):
        db.save_project(Project(name="B", category="IA"))
    db.save_project(a.model_copy(update={"status": "done"}))
    db.save_project(Project(name="B", category="IA"))  # catégorie libérée
    assert db.project_by_category("IA").name == "B"
    db.save_project(Project(name="C"))  # plusieurs projets sans catégorie : permis
    db.save_project(Project(name="D"))


def test_milestones_ordered_and_deleting_detaches_tasks():
    db = Database(":memory:")
    p = Project(name="P")
    db.save_project(p)
    m2 = Milestone(project_id=p.id, title="Rapport", position=1)
    m1 = Milestone(project_id=p.id, title="Modèle", position=0)
    db.save_milestone(m2)
    db.save_milestone(m1)
    assert [m.title for m in db.list_milestones(p.id)] == ["Modèle", "Rapport"]
    t = Task(title="[IA] x", project_id=p.id, milestone_id=m1.id)
    db.upsert_task(t)
    db.delete_milestone(m1.id)
    assert db.get_milestone(m1.id) is None
    got = db.get_task(t.id)
    assert got.milestone_id is None and got.project_id == p.id


def test_task_project_setter_and_listing():
    db = Database(":memory:")
    p = Project(name="P")
    db.save_project(p)
    a, b = Task(title="a"), Task(title="b")
    db.upsert_task(a)
    db.upsert_task(b)
    db.set_task_project(a.id, p.id, None)
    assert db.get_task(a.id).project_id == p.id
    assert [t.id for t in db.list_project_tasks(p.id)] == [a.id]
    db.set_task_project(a.id, None, None)
    assert db.list_project_tasks(p.id) == []


def test_touch_project():
    db = Database(":memory:")
    p = Project(name="P")
    db.save_project(p)
    assert db.get_project(p.id).last_activity is None
    db.touch_project(p.id, dt(2026, 10, 20, 10))
    assert db.get_project(p.id).last_activity == dt(2026, 10, 20, 10)


def test_examples_newest_first_and_limited():
    db = Database(":memory:")
    for i in range(7):
        db.add_project_example(f"titre {i}", "p1", f"m{i}" if i % 2 else None)
    out = db.recent_examples("p1", 5)
    assert [t for t, _ in out] == ["titre 6", "titre 5", "titre 4", "titre 3", "titre 2"]
    assert out[0] == ("titre 6", None) and out[1] == ("titre 5", "m5")
    assert db.recent_examples("autre") == []


def test_proposals_roundtrip():
    db = Database(":memory:")
    assert db.get_proposal("t1") is None
    db.set_proposal("t1", "p1", "m1", "proposed")
    assert db.get_proposal("t1") == {"project_id": "p1", "milestone_id": "m1", "state": "proposed"}
    db.set_proposal("t1", "p1", None, "none")
    assert db.get_proposal("t1")["state"] == "none"


def test_draft_roundtrip_single_row():
    db = Database(":memory:")
    assert db.get_draft() is None
    now = dt(2026, 10, 20, 10)
    db.save_draft("asking", {"name": "X"}, 2, "goal", now)
    d = db.get_draft()
    assert (d["stage"], d["profile"], d["questions_asked"], d["pending"], d["updated_at"]) == (
        "asking", {"name": "X"}, 2, "goal", now)
    db.save_draft("reviewing", {}, 3, None, now)  # remplace la ligne unique
    assert db.get_draft()["stage"] == "reviewing" and db.get_draft()["pending"] is None
    db.clear_draft()
    assert db.get_draft() is None


def test_old_database_is_migrated(tmp_path):
    path = tmp_path / "old.sqlite"
    c = sqlite3.connect(path)
    c.executescript(
        "CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT NOT NULL, due TEXT, estimated_minutes INTEGER, "
        "priority TEXT, status TEXT DEFAULT 'pending', source_id TEXT, reschedule_count INTEGER DEFAULT 0, "
        "created_at TEXT DEFAULT CURRENT_TIMESTAMP);"
        "INSERT INTO tasks (id,title,priority) VALUES ('t1','vieux','medium');"
    )
    c.commit()
    c.close()
    for _ in range(2):  # la seconde ouverture ne doit pas échouer sur « colonne déjà présente »
        db = Database(path)
        t = db.get_task("t1")
        assert t.project_id is None and t.milestone_id is None and t.boost == 0
        assert db.list_projects() == [] and db.get_draft() is None
        db.close()
