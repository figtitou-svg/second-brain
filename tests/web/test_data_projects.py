# tests/web/test_data_projects.py
from __future__ import annotations

from datetime import date, timedelta

from coach.db import Database
from coach.models import Milestone, Project, Suggestion, Task
from tests.coach.conftest import dt
from web import data

NOW = dt(2026, 10, 27, 10, 27)


def setup_project(db, **kw):
    p = Project(name="Projet IA", category="IA", deadline=date(2026, 12, 15), last_activity=NOW,
                profile={"goal": "Entraîner un modèle", "limits": ""}, **kw)
    db.save_project(p)
    ms = [Milestone(project_id=p.id, title=t, position=i, status=s)
          for i, (t, s) in enumerate([("Données", "done"), ("Modèle", "open"), ("Rapport", "open")])]
    for m in ms:
        db.save_milestone(m)
    return p, ms


def callbacks(detail):
    return {a.callback: a for a in detail.actions}


def test_cards_active_then_paused_without_done_projects():
    db = Database(":memory:")
    a, _ = setup_project(db)
    paused = Project(name="Vieux", status="paused")
    done = Project(name="Fini", status="done")
    for p in (paused, done):
        db.save_project(p)
    db.upsert_task(Task(title="[IA] Entraîner", project_id=a.id, due=NOW + timedelta(days=3)))
    cards = data.project_cards(db, NOW)
    assert [c.name for c in cards] == ["Projet IA", "Vieux"]
    c = cards[0]
    assert c.percent == 33 and c.days_left == 49 and c.step == "Modèle" and c.open_tasks == 1
    assert c.next_task == "[IA] Entraîner" and c.color == data.project_colors(db)[a.id] and not c.alert
    assert cards[1].status == "paused"


def test_card_alert_when_project_stalls():
    db = Database(":memory:")
    p = Project(name="Stagne", last_activity=NOW - timedelta(days=12))
    db.save_project(p)
    (card,) = data.project_cards(db, NOW)
    assert card.alert and "12" in card.alert_line


def test_project_detail_missing_returns_none():
    assert data.project_detail(Database(":memory:"), "inconnu", NOW) is None


def test_project_detail_structure_and_actions_for_active_project():
    db = Database(":memory:")
    p, ms = setup_project(db)
    db.upsert_task(Task(title="[IA] Entraîner", project_id=p.id, milestone_id=ms[1].id))
    db.upsert_task(Task(title="[IA] Vrac", project_id=p.id))
    db.save_suggestions([Suggestion(batch="b1", project_id=p.id, title="Idée", created_at=NOW)])
    d = data.project_detail(db, p.id, NOW)
    assert [s.title for s in d.steps] == ["Données", "Modèle", "Rapport"]
    assert [t.title for t in d.steps[1].tasks] == ["[IA] Entraîner"] and d.steps[0].status == "done"
    assert [t.title for t in d.loose] == ["[IA] Vrac"]
    assert [s.title for s in d.suggestions] == ["Idée"]
    assert d.facts == [("Objectif", "Entraîner un modèle")]                 # les champs vides sont omis
    acts = callbacks(d)
    assert f"pm:ok:{ms[1].id}" in acts and acts[f"pm:add:{p.id}"].discuss and acts[f"pp:fix:{p.id}"].discuss
    assert f"pp:pause:{p.id}" in acts and f"pp:end:{p.id}" in acts and f"ps:go:{p.id}" in acts
    assert f"pa:rng:{p.id}" in acts                                          # une tâche sans étape
    assert f"pa:mk:{p.id}" in acts                                           # « Rapport » n'a aucune tâche
    assert f"pp:resume:{p.id}" not in acts


def test_project_detail_states():
    db = Database(":memory:")
    bare = Project(name="Sans rien")
    db.save_project(bare)
    d = data.project_detail(db, bare.id, NOW)
    assert d.steps == [] and d.loose == [] and d.suggestions == []
    assert set(callbacks(d)) == {f"pm:add:{bare.id}", f"pp:fix:{bare.id}", f"pp:pause:{bare.id}",
                                 f"pp:end:{bare.id}", f"ps:go:{bare.id}"}
    paused = Project(name="En pause", status="paused")
    db.save_project(paused)
    acts = callbacks(data.project_detail(db, paused.id, NOW))
    assert f"pp:resume:{paused.id}" in acts and f"pp:pause:{paused.id}" not in acts and f"ps:go:{paused.id}" not in acts
    done = Project(name="Terminé", status="done")
    db.save_project(done)
    assert data.project_detail(db, done.id, NOW).actions == []
