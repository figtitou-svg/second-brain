# tests/web/test_data_lists.py
from __future__ import annotations

from datetime import timedelta

from coach.calendar_client import FakeCalendarClient
from coach.db import Database
from coach.models import Milestone, Project, Task
from coach.rules import load_rules
from coach.scheduler import CoachScheduler
from tests.coach.conftest import FROZEN, dt
from web import data

NOW = dt(2026, 10, 27, 10, 27)


def env():
    db = Database(":memory:")
    sched = CoachScheduler(db, FakeCalendarClient(), load_rules(FROZEN), lambda t, b=None: None, lambda: NOW)
    return db, sched


def test_focus_rows_full_ranking_with_reasons_and_bars():
    db, sched = env()
    p = Project(name="Projet IA")
    db.save_project(p)
    for i in range(6):
        db.upsert_task(Task(title=f"T{i}", project_id=p.id if i == 0 else None, due=NOW + timedelta(days=i + 1)))
    rows = data.focus_rows(sched, NOW)
    assert [r.rank for r in rows] == [1, 2, 3, 4, 5, 6]
    assert rows[0].level == "focus" and rows[3].level != "focus"
    assert rows[0].bar == 100 and all(0 <= r.bar <= 100 for r in rows)
    assert all(r.reason for r in rows)
    assert rows[0].color in {data.project_colors(db)[p.id], data.NO_PROJECT}


def test_focus_rows_empty_and_limit():
    db, sched = env()
    assert data.focus_rows(sched, NOW) == []
    for i in range(5):
        db.upsert_task(Task(title=f"T{i}", due=NOW + timedelta(days=i + 1)))
    assert len(data.focus_rows(sched, NOW, limit=2)) == 2


def test_task_groups_by_project_then_without_project():
    db, _ = env()
    ia, job = Project(name="Projet IA"), Project(name="Job")
    for p in (ia, job):
        db.save_project(p)
    m = Milestone(project_id=ia.id, title="Modèle", position=0)
    db.save_milestone(m)
    db.upsert_task(Task(title="[IA] B", project_id=ia.id, milestone_id=m.id, due=NOW + timedelta(days=5)))
    db.upsert_task(Task(title="[IA] A", project_id=ia.id, due=NOW + timedelta(days=2), status="scheduled"))
    db.upsert_task(Task(title="Job C", project_id=job.id))
    db.upsert_task(Task(title="Libre", due=NOW + timedelta(days=1)))
    groups, counts = data.task_groups(db, NOW, "tout")
    assert [g.name for g in groups] == ["Projet IA", "Job", "Sans projet"]
    ia_group = next(g for g in groups if g.name == "Projet IA")
    assert [r.title for r in ia_group.rows] == ["[IA] A", "[IA] B"]            # par échéance
    assert ia_group.rows[1].step == "Modèle" and ia_group.rows[0].status == "scheduled"
    assert ia_group.color == data.project_colors(db)[ia.id]
    assert counts == {"tout": 4, "retard": 0, "semaine": 3, "sans-projet": 1}


def test_task_groups_hide_done_and_dropped():
    db, _ = env()
    db.upsert_task(Task(title="Ouverte"))
    db.upsert_task(Task(title="Faite", status="done"))
    db.upsert_task(Task(title="Abandonnée", status="dropped"))
    groups, counts = data.task_groups(db, NOW, "tout")
    assert [r.title for g in groups for r in g.rows] == ["Ouverte"] and counts["tout"] == 1


def test_task_groups_filters_and_unknown_filter_falls_back():
    db, _ = env()
    p = Project(name="Projet IA")
    db.save_project(p)
    db.upsert_task(Task(title="Retard", project_id=p.id, due=NOW - timedelta(days=1)))
    db.upsert_task(Task(title="Semaine", due=NOW + timedelta(days=3)))
    db.upsert_task(Task(title="Loin", due=NOW + timedelta(days=30)))
    titles = lambda f: sorted(r.title for g in data.task_groups(db, NOW, f)[0] for r in g.rows)   # noqa: E731
    assert titles("retard") == ["Retard"]
    assert titles("semaine") == ["Semaine"]
    assert titles("sans-projet") == ["Loin", "Semaine"]
    assert titles("n'importe quoi") == titles("tout") == ["Loin", "Retard", "Semaine"]
    assert any(r.overdue for g in data.task_groups(db, NOW, "tout")[0] for r in g.rows)


def test_task_groups_empty():
    db, _ = env()
    assert data.task_groups(db, NOW, "tout") == ([], {"tout": 0, "retard": 0, "semaine": 0, "sans-projet": 0})
