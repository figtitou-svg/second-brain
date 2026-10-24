from __future__ import annotations

from coach import project_assistant, project_service, store
from coach.calendar_client import FakeCalendarClient
from coach.db import Database
from coach.models import Task
from coach.rules import load_rules
from coach.scheduler import CoachScheduler
from tests.coach.assistant_helpers import NOW, FakeLLM, make_project
from tests.coach.conftest import FROZEN


def loose_tasks(db, p, *titles):
    out = []
    for t in titles:
        task = Task(title=f"[IA] {t}", project_id=p.id)
        db.upsert_task(task)
        # created_at est rempli par SQLite à la seconde : sans cela, l'ordre des tâches créées ensemble est aléatoire
        n = db._query("SELECT COUNT(*) AS n FROM tasks")[0]["n"]
        db._exec("UPDATE tasks SET created_at=? WHERE id=?", (f"2026-10-21 09:{n // 60:02d}:{n % 60:02d}", task.id))
        out.append(task)
    return out


def assign(*pairs):
    return {"assign": [{"n": n, "step": s} for n, s in pairs]}


def cbs(buttons):
    return [cb for row in buttons for _, cb in row]


def test_rank_candidates_and_the_detail_button():
    db, p, m1, m2, m3 = make_project()
    assert "pa:rng" not in " ".join(cbs(project_service.detail(db, p.id, NOW)[1]))      # aucune tâche à ranger
    a, b = loose_tasks(db, p, "Nettoyer", "Entraîner")
    db.set_task_project(b.id, p.id, m2.id)
    db.upsert_task(Task(title="[IA] Faite", project_id=p.id, status="done"))
    assert [t.id for t in project_service.rank_candidates(db, p.id)] == [a.id]
    assert f"pa:rng:{p.id}" in cbs(project_service.detail(db, p.id, NOW)[1])
    for m in (m2, m3):
        db.save_milestone(m.model_copy(update={"status": "done"}))
    assert f"pa:rng:{p.id}" not in cbs(project_service.detail(db, p.id, NOW)[1])      # plus d'étape ouverte
    db.save_milestone(m2.model_copy(update={"status": "open"}))
    db.save_project(p.model_copy(update={"status": "paused"}))
    assert f"pa:rng:{p.id}" not in cbs(project_service.detail(db, p.id, NOW)[1])      # projet en pause


def test_rank_batch_proposes_and_renders_the_lot():
    db, p, m1, m2, m3 = make_project()
    a, b, c = loose_tasks(db, p, "Nettoyer le dataset", "Entraîner le réseau", "Rédiger <le> rapport")
    llm = FakeLLM(assign((1, "données"), (2, "Modèle"), (3, "Rapport"), (9, "Modèle"), (2, "Rapport"), (1, "Inconnue")))
    text, buttons = project_assistant.rank_batch(db, llm, "private", p.id, NOW)
    prompt = llm.calls[0]["prompt"]
    assert "1. [IA] Nettoyer le dataset" in prompt and "ouverte : Modèle" in prompt and a.id not in prompt
    assert "🗂 Je propose de ranger les tâches de <b>Projet IA</b>" in text
    assert "→ <i>Modèle</i>" in text and "Rédiger &lt;le&gt; rapport" in text
    assert db.get_proposal(b.id) == {"project_id": p.id, "milestone_id": m2.id, "state": "lot"}
    assert db.get_proposal(c.id)["milestone_id"] == m3.id
    assert db.get_proposal(a.id) is None                # « Données » est déjà franchie : pas de proposition
    c_list = cbs(buttons)
    assert f"pa:ok:{b.id}" in c_list and f"pa:no:{c.id}" in c_list
    assert c_list[-2:] == [f"pa:all:{p.id}", f"pa:none:{p.id}"]
    assert db.get_task(b.id).milestone_id is None        # rien n'est rangé sans clic


def test_rank_batch_failures_and_no_usable_answer_change_nothing():
    db, p, m1, m2, m3 = make_project()
    loose_tasks(db, p, "Nettoyer")
    text, buttons = project_assistant.rank_batch(db, FakeLLM(RuntimeError("quota")), "private", p.id, NOW)
    assert "pas réussi" in text and cbs(buttons) == [f"pjv:{p.id}"]
    text, _ = project_assistant.rank_batch(db, FakeLLM(assign((1, None), (5, "Modèle"))), "private", p.id, NOW)
    assert "pas trouvé d'étape" in text and db.list_lot_proposals(p.id) == []
    text, _ = project_assistant.rank_batch(Database(":memory:"), FakeLLM(), "private", "zzzzzzzz", NOW)
    assert "n'est plus actif" in text
    db.save_project(p.model_copy(update={"status": "paused"}))
    assert "n'est plus actif" in project_assistant.rank_batch(db, FakeLLM(), "private", p.id, NOW)[0]


def test_rank_batch_caps_at_30_sent_and_15_proposed_and_clears_the_previous_lot():
    db, p, m1, m2, m3 = make_project()
    tasks = loose_tasks(db, p, *[f"Tâche {chr(65 + i)}{chr(97 + i)}{i}" for i in range(20)])
    llm = FakeLLM(assign(*[(i, "Modèle") for i in range(1, 21)]))
    text, buttons = project_assistant.rank_batch(db, llm, "private", p.id, NOW)
    assert len(db.list_lot_proposals(p.id)) == 15 and sum(c.startswith("pa:ok:") for c in cbs(buttons)) == 15
    llm2 = FakeLLM(assign((1, "Rapport")))
    project_assistant.rank_batch(db, llm2, "private", p.id, NOW)
    rows = db.list_lot_proposals(p.id)
    assert len(rows) == 1 and rows[0]["milestone_id"] == m3.id                         # l'ancien lot a été effacé


def test_clicks_one_by_one_recompute_the_message():
    db, p, m1, m2, m3 = make_project()
    a, b = loose_tasks(db, p, "Entraîner le réseau", "Rédiger le rapport")
    project_assistant.rank_batch(db, FakeLLM(assign((1, "Modèle"), (2, "Rapport"))), "private", p.id, NOW)
    text, buttons = project_assistant.rank_click(db, "ok", a.id, NOW)
    assert db.get_task(a.id).milestone_id == m2.id and db.get_proposal(a.id)["state"] == "lot_ok"
    assert "1. ✅ " in text and f"pa:ok:{a.id}" not in cbs(buttons) and f"pa:ok:{b.id}" in cbs(buttons)
    assert db.recent_examples(p.id)[0] == ("[IA] Entraîner le réseau", m2.id)
    text, buttons = project_assistant.rank_click(db, "no", b.id, NOW)
    assert db.get_task(b.id).milestone_id is None and db.get_proposal(b.id)["state"] == "lot_no"
    assert "2. ✖ " in text and cbs(buttons) == [f"pjv:{p.id}"] and "1 rangée(s), 1 écartée(s)" in text


def test_all_none_and_idempotence():
    db, p, m1, m2, m3 = make_project()
    a, b, c = loose_tasks(db, p, "Entraîner le réseau", "Rédiger le rapport", "Mesurer la précision")
    project_assistant.rank_batch(db, FakeLLM(assign((1, "Modèle"), (2, "Rapport"), (3, "Modèle"))), "private", p.id, NOW)
    project_assistant.rank_click(db, "no", c.id, NOW)
    text, _ = project_assistant.rank_click(db, "all", p.id, NOW)
    assert (db.get_task(a.id).milestone_id, db.get_task(b.id).milestone_id) == (m2.id, m3.id)
    assert db.get_task(c.id).milestone_id is None
    again, _ = project_assistant.rank_click(db, "ok", a.id, NOW)
    assert again.startswith("👌 Déjà traité") and db.get_task(a.id).milestone_id == m2.id
    assert project_assistant.rank_click(db, "all", p.id, NOW)[0].startswith("👌 Déjà traité")
    assert "Plus de proposition" in project_assistant.rank_click(db, "ok", "zzzzzzzz", NOW)[0]
    d, = loose_tasks(db, p, "Autre tâche distincte")
    project_assistant.rank_batch(db, FakeLLM(assign((2, "Modèle"))), "private", p.id, NOW)    # n° 1 = « Mesurer… » (écartée, toujours sans étape)
    project_assistant.rank_click(db, "none", p.id, NOW)
    assert db.get_task(d.id).milestone_id is None and db.get_proposal(d.id)["state"] == "lot_no"


def test_paused_project_and_vanished_step_block_the_click():
    db, p, m1, m2, m3 = make_project()
    a, b = loose_tasks(db, p, "Entraîner le réseau", "Rédiger le rapport")
    project_assistant.rank_batch(db, FakeLLM(assign((1, "Modèle"), (2, "Rapport"))), "private", p.id, NOW)
    db.save_milestone(m3.model_copy(update={"status": "done"}))              # l'étape est fermée entre-temps
    text, _ = project_assistant.rank_click(db, "ok", b.id, NOW)
    assert "n'existe plus" in text and db.get_task(b.id).milestone_id is None
    assert db.get_proposal(b.id)["state"] == "lot_no"
    db.save_project(p.model_copy(update={"status": "paused"}))
    text, buttons = project_assistant.rank_click(db, "ok", a.id, NOW)
    assert "en pause ou terminé" in text and buttons == [] and db.get_task(a.id).milestone_id is None


def test_store_routes_pa_actions():
    db, p, m1, m2, m3 = make_project()
    sched = CoachScheduler(db, FakeCalendarClient(), load_rules(FROZEN), lambda t, b=None: None, lambda: NOW)
    store.configure(db, sched)
    store.configure_projects(FakeLLM(assign((1, "Modèle"))), "private", {})
    a, = loose_tasks(db, p, "Entraîner le réseau")
    text, buttons = store.assistant_action("pa", f"rng:{p.id}")
    assert "🗂" in text and f"pa:ok:{a.id}" in cbs(buttons)
    store.assistant_action("pa", f"ok:{a.id}")
    assert db.get_task(a.id).milestone_id == m2.id
    assert "Action inconnue" in store.assistant_action("pa", "zz:x")[0]
