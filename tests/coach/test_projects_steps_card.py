from __future__ import annotations

from coach import extract, project_service, store
from coach.calendar_client import FakeCalendarClient
from coach.db import Database
from coach.extract import Extraction
from coach.models import Project, Task
from coach.rules import load_rules
from coach.scheduler import CoachScheduler
from tests.coach.assistant_helpers import NOW, make_project
from tests.coach.conftest import FROZEN


def with_task(**fields):
    db, p, m1, m2, m3 = make_project()
    t = Task(title="[IA] Relire le sujet", project_id=p.id, **fields)
    db.upsert_task(t)
    return db, p, m1, m2, m3, t


def test_propose_step_requires_the_same_project_and_an_open_step():
    db, p, m1, m2, m3, t = with_task()
    assert project_service.propose_step(db, t, "projet ia", "MODÈLE") is True
    assert db.get_proposal(t.id) == {"project_id": p.id, "milestone_id": m2.id, "state": "proposed"}
    other = Task(title="[IA] Autre", project_id=p.id)
    db.upsert_task(other)
    assert project_service.propose_step(db, other, "Un autre projet", "Modèle") is False     # autre projet
    assert project_service.propose_step(db, other, "Projet IA", "Étape inconnue") is False   # étape inconnue
    assert project_service.propose_step(db, other, "Projet IA", "Données") is False          # étape déjà franchie
    assert project_service.propose_step(db, other, "Projet IA", None) is False
    assert db.get_proposal(other.id) is None
    free = Task(title="Sans projet")
    db.upsert_task(free)
    assert project_service.propose_step(db, free, "Projet IA", "Modèle") is False


def test_store_extract_records_step_hints_for_category_tasks(monkeypatch):
    db, p, m1, m2, m3 = make_project()
    sched = CoachScheduler(db, FakeCalendarClient(), load_rules(FROZEN), lambda t, b=None: None, lambda: NOW)
    store.configure(db, sched)
    prefixed = Task(title="[IA] Relire le sujet", source_id="note:1")
    loose = Task(title="Nettoyer le dataset", source_id="note:1")
    hints = {prefixed.id: ("Projet IA", "Rapport"), loose.id: ("Projet IA", "Modèle")}
    monkeypatch.setattr(store.extract, "extract_items", lambda *a, **k: Extraction([prefixed, loose], [], hints))
    store.extract_items("texte", "note:1", "private")
    assert db.get_task(prefixed.id).project_id == p.id
    assert db.get_proposal(prefixed.id) == {"project_id": p.id, "milestone_id": m3.id, "state": "proposed"}
    assert db.get_proposal(loose.id)["milestone_id"] == m2.id                  # tâche sans projet : proposition de projet
    text, buttons = store.project_proposal(prefixed.id)
    assert text == "🧭 Étape <i>Rapport</i> ?"


def test_card_for_a_task_already_in_the_project():
    db, p, m1, m2, m3, t = with_task()
    assert project_service.proposal_card(db, t.id) is None                      # pas de proposition
    db.set_proposal(t.id, p.id, m2.id, "proposed")
    text, buttons = project_service.proposal_card(db, t.id)
    assert text == "🧭 Étape <i>Modèle</i> ?"
    assert buttons == [[("Oui", f"pjok:{t.id}"), ("Autre", f"pjwho:{t.id}"), ("Aucune", f"pjno:{t.id}")]]
    db.set_proposal(t.id, "autreprj", m2.id, "proposed")
    assert project_service.proposal_card(db, t.id) is None                      # vise un autre projet
    db.set_proposal(t.id, p.id, None, "proposed")
    assert project_service.proposal_card(db, t.id) is None                      # pas d'étape proposée
    db.set_proposal(t.id, p.id, m2.id, "proposed")
    db.save_project(p.model_copy(update={"status": "paused"}))
    assert project_service.proposal_card(db, t.id) is None                      # projet en pause
    db.save_project(p)
    db.set_task_project(t.id, p.id, m3.id)
    assert project_service.proposal_card(db, t.id) is None                      # déjà rangée


def test_pjok_sets_the_proposed_step_and_keeps_the_project():
    db, p, m1, m2, m3, t = with_task()
    db.set_proposal(t.id, p.id, m2.id, "proposed")
    text, buttons = project_service.pick(db, "pjok", t.id, None, NOW)
    got = db.get_task(t.id)
    assert (got.project_id, got.milestone_id) == (p.id, m2.id) and "Modèle" in text and buttons == []
    assert db.recent_examples(p.id)[0] == ("[IA] Relire le sujet", m2.id) and db.get_project(p.id).last_activity == NOW
    assert "déjà été traité" in project_service.pick(db, "pjok", t.id, None, NOW)[0]


def test_pjwho_lists_the_open_steps_of_the_task_project_then_pjms_sets_one():
    db, p, m1, m2, m3, t = with_task()
    db.set_proposal(t.id, p.id, m2.id, "proposed")
    text, rows = project_service.pick(db, "pjwho", t.id, None, NOW)
    assert [r[0][1] for r in rows[:-1]] == [f"pjms:{t.id}:{m2.id}", f"pjms:{t.id}:{m3.id}"]   # pas l'étape franchie
    assert rows[-1] == [("Aucune", f"pjno:{t.id}")]
    project_service.pick(db, "pjms", t.id, m3.id, NOW)
    assert db.get_task(t.id).milestone_id == m3.id


def test_step_choice_refuses_foreign_or_closed_steps_and_none_declines():
    db, p, m1, m2, m3, t = with_task()
    other = Project(name="Autre", category="GL")
    db.save_project(other)
    from coach.models import Milestone
    foreign = Milestone(project_id=other.id, title="Étrangère")
    db.save_milestone(foreign)
    for bad in (foreign.id, m1.id, "zzzzzzzz"):
        text, _ = project_service.pick(db, "pjms", t.id, bad, NOW)
        assert "introuvable" in text and db.get_task(t.id).milestone_id is None
    db.set_proposal(t.id, p.id, m2.id, "proposed")
    assert "sans étape" in project_service.pick(db, "pjms", t.id, "-", NOW)[0]
    assert db.get_task(t.id).milestone_id is None and db.get_proposal(t.id)["state"] == "none"
    assert "déjà été traité" in project_service.pick(db, "pjok", t.id, None, NOW)[0]


def test_pjok_without_a_matching_proposal_and_paused_project():
    db, p, m1, m2, m3, t = with_task()
    assert "Plus de proposition" in project_service.pick(db, "pjok", t.id, None, NOW)[0]
    db.set_proposal(t.id, "autreprj", m2.id, "proposed")                       # proposition d'un autre projet
    assert "Plus de proposition" in project_service.pick(db, "pjok", t.id, None, NOW)[0]
    db.set_proposal(t.id, p.id, m2.id, "proposed")
    db.save_project(p.model_copy(update={"status": "paused"}))
    assert "n'est plus actif" in project_service.pick(db, "pjok", t.id, None, NOW)[0]
    assert db.get_task(t.id).milestone_id is None


def test_tasks_without_a_project_keep_the_old_behavior():
    db, p, m1, m2, m3 = make_project()
    t = Task(title="Nettoyer")
    db.upsert_task(t)
    db.set_proposal(t.id, p.id, m2.id, "proposed")
    text, _ = project_service.proposal_card(db, t.id)
    assert text == "📁 <b>Projet IA</b> · étape <i>Modèle</i> ?"
    project_service.pick(db, "pjok", t.id, None, NOW)
    assert (db.get_task(t.id).project_id, db.get_task(t.id).milestone_id) == (p.id, m2.id)
