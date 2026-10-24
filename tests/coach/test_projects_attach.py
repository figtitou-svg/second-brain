from __future__ import annotations

import json

import pytest

import notion_sync
from coach import extract, project_service, store
from coach.calendar_client import FakeCalendarClient
from coach.db import Database
from coach.extract import Extraction, ExtractCache
from coach.models import Milestone, Project, Task
from coach.rules import load_rules
from coach.scheduler import CoachScheduler
from notion_sync import Row
from tests.coach.conftest import FROZEN, dt

NOW = dt(2026, 10, 20, 10)


def make_env():
    db = Database(":memory:")
    s = CoachScheduler(db, FakeCalendarClient(), load_rules(FROZEN), lambda t, b=None: None, lambda: NOW)
    store.configure(db, s)
    ia = Project(name="Projet IA", category="IA")
    db.save_project(ia)
    m1 = Milestone(project_id=ia.id, title="Données", position=0)
    m2 = Milestone(project_id=ia.id, title="Modèle", position=1)
    db.save_milestone(m1)
    db.save_milestone(m2)
    return db, s, ia, m1, m2


def test_attach_by_category_active_and_paused_only_when_prefix_matches():
    db, s, ia, *_ = make_env()
    t = project_service.attach_by_category(db, Task(title="[IA] TP"))
    assert t.project_id == ia.id
    assert project_service.attach_by_category(db, Task(title="sans préfixe")).project_id is None
    db.save_project(ia.model_copy(update={"status": "paused"}))
    assert project_service.attach_by_category(db, Task(title="[IA] TP")).project_id == ia.id
    db.save_project(ia.model_copy(update={"status": "done"}))
    assert project_service.attach_by_category(db, Task(title="[IA] TP")).project_id is None


def test_attach_open_tasks_skips_finished_and_already_attached():
    db = Database(":memory:")
    p = Project(name="IA", category="IA")
    other = Project(name="Autre", category="GL")
    db.save_project(p)
    db.save_project(other)
    a = Task(title="[IA] a", status="pending")
    b = Task(title="[IA] b", status="scheduled")
    done = Task(title="[IA] c", status="done")
    taken = Task(title="[IA] d", project_id=other.id)
    noprefix = Task(title="rien")
    for t in (a, b, done, taken, noprefix):
        db.upsert_task(t)
    assert project_service.attach_open_tasks(db, p) == 2
    assert db.get_task(a.id).project_id == p.id and db.get_task(b.id).project_id == p.id
    assert db.get_task(done.id).project_id is None and db.get_task(taken.id).project_id == other.id
    assert project_service.attach_open_tasks(db, Project(name="Sans catégorie")) == 0


def test_extraction_context_lists_open_milestones_and_examples():
    db, s, ia, m1, m2 = make_env()
    db.save_milestone(m1.model_copy(update={"status": "done"}))
    db.add_project_example("Entraîner le réseau", ia.id, m2.id)
    db.save_project(Project(name="En pause", category="GL", status="paused"))
    ctx = project_service.extraction_context(db)
    assert ctx == [{"name": "Projet IA", "category": "IA", "milestones": ["Modèle"],
                    "examples": [("Entraîner le réseau", "Modèle")]}]


class FakeLLM:
    def __init__(self, tasks):
        self.tasks, self.calls = tasks, []

    def __call__(self, prompt, *, sensitivity, system=None, json_mode=False):
        self.calls.append((prompt, system))
        return json.dumps({"tasks": self.tasks, "events": []})


def run_extract(llm, projects=None, cache=None):
    return extract.extract_items("Il faut nettoyer le dataset", "note:1", "private", llm=llm,
                                 cache=cache or ExtractCache(), now=NOW, projects=projects)


CTX = [{"name": "Projet IA", "category": "IA", "milestones": ["Données", "Modèle"],
        "examples": [("Entraîner le réseau", "Modèle")]}]


def test_extract_without_projects_is_unchanged():
    llm = FakeLLM([{"title": "Nettoyer le dataset"}])
    res = run_extract(llm)
    assert "Projets actifs" not in llm.calls[0][0] and res.hints == {}


def test_extract_with_projects_adds_context_and_returns_hints():
    llm = FakeLLM([{"title": "Nettoyer le dataset", "project": "Projet IA", "milestone": "Données"},
                   {"title": "Acheter du pain"}])
    res = run_extract(llm, CTX)
    prompt, system = llm.calls[0]
    assert "Projets actifs de l'utilisateur" in prompt and "Projet IA (catégorie IA)" in prompt
    assert "étapes ouvertes : Données, Modèle" in prompt and "« Entraîner le réseau » → Modèle" in prompt
    assert "project" in system
    ids = {t.title: t.id for t in res.tasks}
    assert res.hints == {ids["Nettoyer le dataset"]: ("Projet IA", "Données")}


def test_extract_cache_key_changes_with_projects():
    cache = ExtractCache()
    llm = FakeLLM([{"title": "Nettoyer le dataset"}])
    run_extract(llm, CTX, cache)
    run_extract(llm, CTX, cache)
    assert len(llm.calls) == 1                         # mêmes projets : cache
    run_extract(llm, [{**CTX[0], "milestones": ["Rapport"]}], cache)
    assert len(llm.calls) == 2                         # projets modifiés : nouvel appel
    run_extract(llm, None, cache)
    assert len(llm.calls) == 3                         # sans projets : autre clé


def test_store_extract_auto_attaches_by_prefix_and_records_proposals(monkeypatch):
    db, s, ia, m1, m2 = make_env()
    prefixed = Task(title="[IA] Relire le sujet", source_id="note:1")
    loose = Task(title="Nettoyer le dataset", source_id="note:1")
    unknown = Task(title="Autre chose", source_id="note:1")
    hints = {loose.id: ("projet ia", "DONNÉES"), unknown.id: ("Projet inexistant", None)}
    seen = {}

    def fake_extract(text, source_id, sensitivity, **kw):
        seen["projects"] = kw.get("projects")
        return Extraction([prefixed, loose, unknown], [], hints)

    monkeypatch.setattr(store.extract, "extract_items", fake_extract)
    tasks, events = store.extract_items("texte", "note:1", "private")
    assert seen["projects"][0]["name"] == "Projet IA"
    assert db.get_task(prefixed.id).project_id == ia.id
    assert db.get_task(loose.id).project_id is None
    assert db.get_proposal(loose.id) == {"project_id": ia.id, "milestone_id": m1.id, "state": "proposed"}
    assert db.get_proposal(unknown.id) is None and db.get_proposal(prefixed.id) is None


def test_proposal_card_conditions():
    db, s, ia, m1, m2 = make_env()
    t = Task(title="Nettoyer")
    db.upsert_task(t)
    assert project_service.proposal_card(db, t.id) is None            # pas de proposition
    db.set_proposal(t.id, ia.id, m1.id, "proposed")
    text, buttons = project_service.proposal_card(db, t.id)
    assert "Projet IA" in text and "Données" in text
    assert buttons == [[("Oui", f"pjok:{t.id}"), ("Autre", f"pjwho:{t.id}"), ("Aucun", f"pjno:{t.id}")]]
    db.save_project(ia.model_copy(update={"status": "paused"}))
    assert project_service.proposal_card(db, t.id) is None            # projet en pause : jamais proposé
    db.save_project(ia)
    db.set_proposal(t.id, ia.id, m1.id, "none")
    assert project_service.proposal_card(db, t.id) is None
    db.set_proposal(t.id, ia.id, m1.id, "proposed")
    db.set_task_project(t.id, ia.id, m2.id)
    assert project_service.proposal_card(db, t.id) is None            # déjà rangée (projet et étape)


def test_pick_yes_attaches_remembers_and_touches():
    db, s, ia, m1, m2 = make_env()
    t = Task(title="Nettoyer")
    db.upsert_task(t)
    db.set_proposal(t.id, ia.id, m1.id, "proposed")
    text, buttons = project_service.pick(db, "pjok", t.id, None, NOW)
    assert "Projet IA" in text and "Données" in text and buttons == []
    got = db.get_task(t.id)
    assert (got.project_id, got.milestone_id) == (ia.id, m1.id)
    assert db.recent_examples(ia.id) == [("Nettoyer", m1.id)]
    assert db.get_project(ia.id).last_activity == NOW
    again, _ = project_service.pick(db, "pjok", t.id, None, NOW)       # clic tardif
    assert "déjà été traité" in again and len(db.recent_examples(ia.id)) == 1


def test_pick_yes_with_deleted_milestone_attaches_without_milestone():
    db, s, ia, m1, m2 = make_env()
    t = Task(title="Nettoyer")
    db.upsert_task(t)
    db.set_proposal(t.id, ia.id, m1.id, "proposed")
    db.delete_milestone(m1.id)                                         # proposition périmée
    text, buttons = project_service.pick(db, "pjok", t.id, None, NOW)
    assert "Projet IA" in text and buttons == []
    got = db.get_task(t.id)
    assert (got.project_id, got.milestone_id) == (ia.id, None)
    assert db.recent_examples(ia.id) == [("Nettoyer", None)]


def test_pick_other_project_then_milestone_then_none_flow():
    db, s, ia, m1, m2 = make_env()
    gl = Project(name="Génie logiciel", category="GL")
    db.save_project(gl)
    t = Task(title="Nettoyer")
    db.upsert_task(t)
    db.set_proposal(t.id, ia.id, m1.id, "proposed")
    text, rows = project_service.pick(db, "pjwho", t.id, None, NOW)
    labels = [r[0][0] for r in rows[:-1]]
    assert labels == ["Projet IA", "Génie logiciel"] and rows[-1][0][1] == f"pjno:{t.id}"
    assert rows[0][0][1] == f"pjset:{t.id}:{ia.id}"
    text, rows = project_service.pick(db, "pjset", t.id, ia.id, NOW)
    assert [r[0][1] for r in rows[:-1]] == [f"pjms:{t.id}:{m1.id}", f"pjms:{t.id}:{m2.id}"]
    assert rows[-1][0][1] == f"pjms:{t.id}:-"
    project_service.pick(db, "pjms", t.id, "-", NOW)                  # sans étape
    got = db.get_task(t.id)
    assert (got.project_id, got.milestone_id) == (ia.id, None)
    # un projet sans étape ouverte rattache directement
    t2 = Task(title="Autre")
    db.upsert_task(t2)
    db.set_proposal(t2.id, ia.id, None, "proposed")
    project_service.pick(db, "pjset", t2.id, gl.id, NOW)
    assert db.get_task(t2.id).project_id == gl.id


def test_pick_none_marks_the_task_and_stops_proposing():
    db, s, ia, m1, m2 = make_env()
    t = Task(title="Nettoyer")
    db.upsert_task(t)
    db.set_proposal(t.id, ia.id, m1.id, "proposed")
    text, _ = project_service.pick(db, "pjno", t.id, None, NOW)
    assert "hors projet" in text and db.get_task(t.id).project_id is None
    assert project_service.proposal_card(db, t.id) is None
    assert "déjà été traité" in project_service.pick(db, "pjok", t.id, None, NOW)[0]


def test_notion_new_and_renamed_tasks_join_the_project():
    db, s, ia, *_ = make_env()
    rows = [Row("aaaa-1", "Terminer TP3", "Algorithmique avancée pour l'IA et les graphes", "2026-10-30", "À faire"),
            Row("bbbb-2", "Autre", "EPS", "2026-10-30", "À faire")]

    class FakeNotion:
        def __init__(self, rows):
            self.rows = rows

        def fetch_all(self):
            return self.rows

        def set_done(self, page_id):
            pass

    notion_sync.sync(db, FakeNotion(rows), lambda t, b: None, lambda tid: None, lambda i: [], s)
    assert db.get_task(notion_sync.task_id("aaaa-1")).project_id == ia.id
    assert db.get_task(notion_sync.task_id("bbbb-2")).project_id is None
    # titre modifié côté Notion : la catégorie change, la tâche rejoint le projet
    db.save_project(Project(name="Sport", category="EPS"))
    rows[1] = Row("bbbb-2", "Autre bis", "EPS", "2026-10-30", "À faire")
    notion_sync.sync(db, FakeNotion(rows), lambda t, b: None, lambda tid: None, lambda i: [], s)
    assert db.get_task(notion_sync.task_id("bbbb-2")).project_id is not None


def test_split_task_parts_inherit_project_and_milestone():
    db, s, ia, m1, m2 = make_env()
    t = Task(title="[IA] Gros", estimated_minutes=120, due=dt(2026, 10, 30, 18), project_id=ia.id,
             milestone_id=m1.id)
    s.schedule_tasks([t])
    s.split_task(t.id)
    parts = [x for x in db.list_tasks() if x.title.startswith("[IA] Gros (")]
    assert len(parts) == 2 and all((x.project_id, x.milestone_id) == (ia.id, m1.id) for x in parts)


def test_extract_non_string_project_or_milestone_does_not_abandon_the_whole_extraction():
    llm = FakeLLM([{"title": "Nettoyer le dataset", "project": "Projet IA", "milestone": "Données"},
                   {"title": "Acheter du pain", "project": False},
                   {"title": "Relire le rapport", "project": "Projet IA", "milestone": 0},
                   {"title": "Appeler la banque", "project": {}, "milestone": []}])
    res = run_extract(llm, CTX)
    assert len(llm.calls) == 1                                   # pas de nouvel essai
    ids = {t.title: t.id for t in res.tasks}
    assert set(ids) == {"Nettoyer le dataset", "Acheter du pain", "Relire le rapport", "Appeler la banque"}
    assert res.hints == {ids["Nettoyer le dataset"]: ("Projet IA", "Données"),
                         ids["Relire le rapport"]: ("Projet IA", None)}
