# tests/web/test_data_step3.py
from __future__ import annotations

from datetime import timedelta

from coach.calendar_client import FakeCalendarClient
from coach.db import Database
from coach.models import Block, Event, Milestone, Project, Suggestion, Task
from coach.rules import load_rules
from coach.scheduler import CoachScheduler
from tests.coach.conftest import FROZEN, dt
from web import data

NOW = dt(2026, 10, 28, 10, 27)


class FakeNotes:
    def __init__(self):
        self.docs = {"n1": {"id": "n1", "title": "Code garage", "kind": "note", "sensitivity": "private", "source": "telegram",
                            "created_at": NOW, "folder": None, "excerpt": "Le code est 4721", "body": "Le code est 4721.\nSuite."},
                     "n2": {"id": "n2", "title": "Stage", "kind": "mail", "sensitivity": "public", "source": "gmail",
                            "created_at": NOW - timedelta(days=1), "folder": None, "excerpt": "rendez-vous", "body": "rendez-vous mardi"}}

    def recent(self, limit=50):
        return list(self.docs.values())[:limit]

    def search(self, query, k=20):
        return [d for d in self.docs.values() if query.lower() in d["title"].lower() or query.lower() in d["body"].lower()][:k]

    def get(self, doc_id):
        return self.docs.get(doc_id)


def make(notes=None):
    db = Database(":memory:")
    cal = FakeCalendarClient()
    sched = CoachScheduler(db, cal, load_rules(FROZEN), lambda t, b=None: None, lambda: NOW)
    return db, data.Views(db, sched, data.AgendaCache(cal.agenda), clock=lambda: NOW, notes=notes)


def test_a_trier_events_and_unplanned_pending_tasks():
    db, v = make()
    db.add_event_if_new(Event(title="Dentiste", start=dt(2026, 10, 30, 9), end=dt(2026, 10, 30, 10), location="Lyon"))
    db.add_event_if_new(Event(title="Déjà ajouté", start=dt(2026, 10, 31, 9), end=dt(2026, 10, 31, 10)))
    db.set_event_status([e for e in db.all_events() if e.title == "Déjà ajouté"][0].id, "added")
    late, soon, none = (Task(title="Sans date"), Task(title="Bientôt", due=NOW + timedelta(days=1)),
                        Task(title="Plus tard", due=NOW + timedelta(days=5)))
    planned, done = Task(title="Planifiée", status="scheduled"), Task(title="Faite", status="done")
    placed = Task(title="Avec un bloc ouvert")
    for t in (late, soon, none, planned, done, placed):
        db.upsert_task(t)
    db.add_block(Block(task_id=placed.id, start=NOW + timedelta(hours=3), end=NOW + timedelta(hours=4)))
    r = v.a_trier()
    assert [e.title for e in r["events"]] == ["Dentiste"] and r["events"][0].location == "Lyon"
    assert [t.title for t in r["tasks"]] == ["Bientôt", "Plus tard", "Sans date"]          # échéance, sans date en dernier


def test_a_trier_empty():
    _, v = make()
    assert v.a_trier() == {"events": [], "tasks": []}


def test_suggestions_three_kinds_and_total():
    db, v = make()
    p = Project(name="Projet IA", last_activity=NOW)
    paused = Project(name="En pause", status="paused")
    for x in (p, paused):
        db.save_project(x)
    m = Milestone(project_id=p.id, title="Modèle", position=0)
    db.save_milestone(m)
    db.save_suggestions([Suggestion(batch="b1", project_id=p.id, milestone_id=m.id, title="Idée A", estimated_minutes=40, created_at=NOW),
                         Suggestion(batch="b1", project_id=p.id, title="Idée B", created_at=NOW, state="added")])
    loose = Task(title="[IA] Vrac", project_id=p.id)
    db.upsert_task(loose)
    db.set_proposal(loose.id, p.id, m.id, "lot")
    other = Task(title="Nouvelle tâche")
    db.upsert_task(other)
    db.set_proposal(other.id, p.id, None, "proposed")
    s = v.suggestions()
    assert [(b.project_name, [i.title for i in b.items]) for b in s["batches"]] == [("Projet IA", ["Idée A"])]
    assert s["batches"][0].batch_id == "b1" and s["batches"][0].items[0].step == "Modèle"
    assert [(l.project_name, [r.title for r in l.rows]) for l in s["lots"]] == [("Projet IA", ["[IA] Vrac"])]
    assert s["lots"][0].rows[0].step == "Modèle"
    assert [p_.task_title for p_ in s["proposals"]] == ["Nouvelle tâche"] and s["proposals"][0].buttons
    assert s["total"] == 3


def test_suggestions_empty():
    _, v = make()
    s = v.suggestions()
    assert s["batches"] == s["lots"] == s["proposals"] == [] and s["total"] == 0


def test_notifications_grouped_by_day_newest_first():
    db, v = make()
    db.log_notification("ancien", [], True, NOW - timedelta(days=2))
    db.log_notification("matin", [[("Fait", "ok:b1")]], True, NOW - timedelta(hours=3))
    db.log_notification("midi", [], False, NOW)
    groups = v.notifications()
    assert [g.label for g in groups] == ["mercredi 28 octobre", "lundi 26 octobre"]
    assert [i.text for i in groups[0].items] == ["midi", "matin"]
    assert groups[0].items[0].delivered is False and groups[0].items[1].buttons == [[("Fait", "ok:b1")]]
    assert make()[1].notifications() == []


def test_notes_list_search_and_one_note():
    _, v = make(FakeNotes())
    r = v.notes("")
    assert [d.title for d in r["docs"]] == ["Code garage", "Stage"] and r["searched"] is False
    r = v.notes("garage")
    assert [d.id for d in r["docs"]] == ["n1"] and r["searched"] is True and r["query"] == "garage"
    assert v.notes("x" * 500)["query"] == "x" * 200
    full = v.note("n1")
    assert full.body.startswith("Le code est 4721") and full.sensitivity == "private"
    assert v.note("inconnu") is None
    assert make()[1].notes("") == {"query": "", "docs": [], "searched": False} and make()[1].note("n1") is None


def test_badges_count_what_waits_for_the_user():
    db, v = make()
    p = Project(name="Stagne", last_activity=NOW - timedelta(days=12))
    db.save_project(p)
    db.upsert_task(Task(title="À planifier"))
    db.upsert_task(Task(title="Planifiée", status="scheduled"))
    db.add_event_if_new(Event(title="RDV", start=dt(2026, 10, 30, 9), end=dt(2026, 10, 30, 10)))
    db.save_suggestions([Suggestion(batch="b1", project_id=p.id, title="Idée", created_at=NOW)])
    assert v.badges() == {"a_trier": 2, "suggestions": 1, "taches": 2, "projets_alerte": True}


def test_badges_never_raise():
    db, v = make()
    db.close()
    assert v.badges() == {}


def test_naive_note_dates_become_aware_in_scheduler_tz():
    naive = NOW.replace(tzinfo=None)
    notes = FakeNotes()
    notes.docs["n1"]["created_at"] = naive
    _, v = make(notes)
    assert all(d.created_at.tzinfo is not None for d in v.notes("")["docs"])
    assert v.note("n1").created_at.utcoffset() == v.sched.tz.utcoffset(naive)
    assert v.note("n2").created_at == NOW - timedelta(days=1)


def test_badge_suggestions_equals_page_total_on_mixed_fixture():
    db, v = make()
    p = Project(name="Projet", last_activity=NOW)
    db.save_project(p)
    m = Milestone(project_id=p.id, title="Étape", position=0)
    db.save_milestone(m)
    db.save_suggestions([Suggestion(batch="b", project_id=p.id, title="A", created_at=NOW),
                         Suggestion(batch="b", project_id=p.id, title="B", created_at=NOW)])
    for i in range(2):
        t = Task(title=f"lot{i}", project_id=p.id)
        db.upsert_task(t)
        db.set_proposal(t.id, p.id, m.id, "lot")
    ghost = Task(title="fantôme", status="done")          # proposition sans carte : ni page ni pastille
    db.upsert_task(ghost)
    db.set_proposal(ghost.id, p.id, None, "proposed")
    t = Task(title="libre")
    db.upsert_task(t)
    db.set_proposal(t.id, p.id, None, "proposed")
    total = v.suggestions()["total"]
    assert total == 5 and v.badges()["suggestions"] == total

