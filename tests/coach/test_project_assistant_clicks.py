from __future__ import annotations

import threading
import time

from coach import project_assistant, store
from coach.calendar_client import FakeCalendarClient
from coach.db import Database
from coach.models import Block, Project, Suggestion, Task
from coach.rules import load_rules
from coach.scheduler import CoachScheduler
from tests.coach.assistant_helpers import NOW, make_project
from tests.coach.conftest import FROZEN, dt


def lot(db, p, m, *titles, batch="lot00001", minutes=90):
    items = [Suggestion(batch=batch, project_id=p.id, milestone_id=m.id if m else None, title=t,
                        estimated_minutes=minutes, created_at=NOW) for t in titles]
    db.save_suggestions(items)
    return items


def callbacks(buttons):
    return [cb for row in buttons for _, cb in row]


def test_render_shape_and_escaping():
    db, p, m1, m2, m3 = make_project()
    a, b = lot(db, p, m2, "Entraîner <un> modèle", "Mesurer la précision")
    text, buttons = project_assistant.render_batch(db, "lot00001")
    assert text.startswith("💡 Pour la suite de <b>Projet IA</b> (étape <i>Modèle</i>), je te propose :")
    assert "1. Entraîner &lt;un&gt; modèle · 1 h 30" in text and "2. Mesurer la précision · 1 h 30" in text
    assert buttons[0] == [("➕ 1", f"ps:add:{a.id}"), ("📌 1", f"ps:plan:{a.id}"), ("✖ 1", f"ps:no:{a.id}")]
    assert buttons[1][0][1] == f"ps:add:{b.id}"
    assert buttons[-1] == [("Tout ajouter", "ps:all:lot00001"), ("Rien", "ps:none:lot00001")]
    assert "introuvable" in project_assistant.render_batch(db, "zzz")[0]


def test_add_creates_a_real_task_with_the_right_fields():
    db, p, m1, m2, m3 = make_project()
    a, = lot(db, p, m2, "Entraîner un modèle")
    text, buttons = project_assistant.click(db, "add", a.id, NOW)
    task, = [t for t in db.list_tasks() if t.source_id == f"suggestion:{a.id}"]
    assert task.title == "[IA] Entraîner un modèle" and task.estimated_minutes == 90 and task.priority == "medium"
    assert (task.project_id, task.milestone_id, task.status) == (p.id, m2.id, "pending")
    assert task.due == dt(2026, 11, 20, 23, 59)                        # date de l'étape
    assert db.get_suggestion(a.id).state == "added" and "1. ✅ Entraîner un modèle" in text
    assert db.get_project(p.id).last_activity == NOW and db.recent_examples(p.id)[0][0] == "[IA] Entraîner un modèle"
    assert callbacks(buttons) == [f"pjv:{p.id}"]                         # tout est traité : bouton « Voir le projet »


def test_task_fields_without_step_date_or_category():
    db, p, m1, m2, m3 = make_project()
    a, = lot(db, p, m3, "Rédiger le rapport")                            # étape Rapport : pas de date -> date de rendu
    project_assistant.click(db, "add", a.id, NOW)
    task, = [t for t in db.list_tasks() if t.source_id == f"suggestion:{a.id}"]
    assert task.due == dt(2026, 12, 15, 23, 59)
    db2 = Database(":memory:")
    free = Project(name="Perso")
    db2.save_project(free)
    b, = lot(db2, free, None, "[Guitare] Apprendre trois accords")
    project_assistant.click(db2, "add", b.id, NOW)
    t2, = db2.list_tasks()
    assert t2.title == "[Guitare] Apprendre trois accords" and t2.due is None and t2.milestone_id is None


def test_plan_places_the_task_or_says_there_is_no_slot():
    db, p, m1, m2, m3 = make_project()
    a, b = lot(db, p, m2, "Entraîner un modèle", "Mesurer la précision")
    planned = []
    text, _ = project_assistant.click(db, "plan", a.id, NOW, plan=lambda tid: planned.append(tid) or [
        Block(task_id=tid, start=dt(2026, 10, 22, 9), end=dt(2026, 10, 22, 10))])
    assert db.get_suggestion(a.id).state == "planned" and "📌 Planifiée" in text and "09:00–10:00" in text
    assert planned == [db.list_tasks()[0].id]
    text, _ = project_assistant.click(db, "plan", b.id, NOW, plan=lambda tid: [])
    assert db.get_suggestion(b.id).state == "added" and "aucun créneau" in text


def test_double_tap_on_plan_plans_only_once():
    db, p, m1, m2, m3 = make_project()
    a, = lot(db, p, m2, "Entraîner un modèle")
    planned, results = [], []

    def slow_plan(tid):
        planned.append(tid)
        time.sleep(0.3)     # le second clic arrive pendant la planification du premier
        return [Block(task_id=tid, start=dt(2026, 10, 22, 9), end=dt(2026, 10, 22, 10))]

    threads = [threading.Thread(target=lambda: results.append(project_assistant.click(db, "plan", a.id, NOW, plan=slow_plan)))
               for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(planned) == 1 and len(db.list_tasks()) == 1 and db.get_suggestion(a.id).state == "planned"
    assert sorted("Déjà traité" in text for text, _ in results) == [False, True]


def test_plan_task_reuses_the_blocks_a_task_already_has():
    db, p, m1, m2, m3 = make_project()
    sched = CoachScheduler(db, FakeCalendarClient(), load_rules(FROZEN), lambda t, b=None: None, lambda: NOW)
    store.configure(db, sched)
    a, = lot(db, p, m2, "Entraîner un modèle", minutes=60)
    project_assistant.click(db, "add", a.id, NOW)
    task, = db.list_tasks()
    first = store._plan_task(task.id)
    again = store._plan_task(task.id)
    assert first and [b.id for b in again] == [b.id for b in first]
    assert len(db.list_blocks(task.id, open_only=True)) == len(first)


def test_dismiss_all_and_none():
    db, p, m1, m2, m3 = make_project()
    a, b, c = lot(db, p, m2, "Alpha pipeline", "Bravo tests", "Charlie deploy")
    project_assistant.click(db, "no", a.id, NOW)
    assert db.get_suggestion(a.id).state == "dismissed" and db.list_tasks() == []
    text, _ = project_assistant.click(db, "all", "lot00001", NOW)
    assert [db.get_suggestion(s.id).state for s in (a, b, c)] == ["dismissed", "added", "added"]
    assert len(db.list_tasks()) == 2 and "2 ajoutée(s), 0 planifiée(s), 1 écartée(s)" in text
    d, e = lot(db, p, m2, "Delta docs", "Echo bench", batch="lot00002")
    project_assistant.click(db, "none", "lot00002", NOW)
    assert [db.get_suggestion(s.id).state for s in (d, e)] == ["dismissed", "dismissed"] and len(db.list_tasks()) == 2


def test_late_or_double_click_changes_nothing():
    db, p, m1, m2, m3 = make_project()
    a, b = lot(db, p, m2, "Alpha pipeline", "Bravo tests")
    project_assistant.click(db, "add", a.id, NOW)
    text, buttons = project_assistant.click(db, "add", a.id, NOW)
    assert text.startswith("👌 Déjà traité") and len(db.list_tasks()) == 1
    assert callbacks(buttons) == [f"ps:add:{b.id}", f"ps:plan:{b.id}", f"ps:no:{b.id}", "ps:all:lot00001", "ps:none:lot00001"]
    project_assistant.click(db, "none", "lot00001", NOW)
    assert project_assistant.click(db, "all", "lot00001", NOW)[0].startswith("👌 Déjà traité")
    assert "introuvable" in project_assistant.click(db, "add", "zzzzzzzz", NOW)[0]
    assert "introuvable" in project_assistant.click(db, "all", "zzzzzzzz", NOW)[0]


def test_existing_similar_task_is_not_duplicated():
    db, p, m1, m2, m3 = make_project()
    db.upsert_task(Task(title="[IA] Entraîner un modèle", project_id=p.id))
    a, = lot(db, p, m2, "Entraîner un modèle")
    text, _ = project_assistant.click(db, "add", a.id, NOW)
    assert len(db.list_tasks()) == 1 and "déjà dans tes tâches" in text and db.get_suggestion(a.id).state == "added"


def test_planner_failure_keeps_one_task_and_says_no_slot():
    db, p, m1, m2, m3 = make_project()
    a, = lot(db, p, m2, "Entraîner un modèle")

    def boom(tid):
        raise RuntimeError("calendrier indisponible")

    text, _ = project_assistant.click(db, "plan", a.id, NOW, plan=boom)
    assert len(db.list_tasks()) == 1 and db.get_suggestion(a.id).state == "added" and "aucun créneau" in text


def test_interrupted_click_reuses_the_task_already_created():
    db, p, m1, m2, m3 = make_project()
    a, b = lot(db, p, m2, "Alpha pipeline", "Bravo tests")
    project_assistant.create_task(db, p, a, NOW)                         # clic interrompu : tâche créée, suggestion restée proposed
    project_assistant.create_task(db, p, b, NOW)
    existing = {t.source_id: t.id for t in db.list_tasks()}
    assert db.get_suggestion(a.id).state == "proposed" and len(existing) == 2
    text, _ = project_assistant.click(db, "add", a.id, NOW)
    assert db.get_suggestion(a.id).state == "added" and len(db.list_tasks()) == 2 and "déjà dans tes tâches" not in text
    planned = []
    text, _ = project_assistant.click(db, "plan", b.id, NOW, plan=lambda tid: planned.append(tid) or [
        Block(task_id=tid, start=dt(2026, 10, 22, 9), end=dt(2026, 10, 22, 10))])
    assert planned == [existing[f"suggestion:{b.id}"]] and len(db.list_tasks()) == 2
    assert db.get_suggestion(b.id).state == "planned" and "📌 Planifiée" in text


def test_duplicate_titles_in_one_batch_create_a_single_task():
    db, p, m1, m2, m3 = make_project()
    a, b = lot(db, p, m2, "Alpha pipeline", "Alpha pipeline")
    project_assistant.click(db, "all", "lot00001", NOW)
    assert len(db.list_tasks()) == 1
    assert [db.get_suggestion(s.id).state for s in (a, b)] == ["added", "added"]


def test_paused_or_finished_project_blocks_the_clicks():
    db, p, m1, m2, m3 = make_project()
    a, = lot(db, p, m2, "Alpha pipeline")
    db.save_project(p.model_copy(update={"status": "paused"}))
    text, buttons = project_assistant.click(db, "add", a.id, NOW)
    assert "en pause ou terminé" in text and buttons == [] and db.list_tasks() == []
    assert db.get_suggestion(a.id).state == "proposed"
    assert "en pause ou terminé" in project_assistant.click(db, "all", "lot00001", NOW)[0]


def test_store_routes_ps_clicks_and_plans_with_the_scheduler():
    db, p, m1, m2, m3 = make_project()
    sched = CoachScheduler(db, FakeCalendarClient(), load_rules(FROZEN), lambda t, b=None: None, lambda: NOW)
    store.configure(db, sched)
    a, = lot(db, p, m2, "Entraîner un modèle", minutes=60)
    text, _ = store.assistant_action("ps", f"plan:{a.id}")
    task, = db.list_tasks()
    assert db.get_suggestion(a.id).state == "planned" and "📌 Planifiée" in text
    assert db.list_blocks(task.id, open_only=True)
    assert "Action inconnue" in store.assistant_action("zz", "x")[0]
