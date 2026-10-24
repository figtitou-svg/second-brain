from __future__ import annotations

import json
from datetime import date

from coach import project_service, store
from coach.calendar_client import FakeCalendarClient
from coach.db import Database
from coach.models import Milestone, Project, Task
from coach.project_interview import Interview
from coach.rules import load_rules
from coach.scheduler import CoachScheduler
from tests.coach.conftest import FROZEN, dt

NOW = dt(2026, 10, 20, 10)


def make_db():
    db = Database(":memory:")
    p = Project(name="Projet IA", category="IA", deadline=date(2026, 12, 15), profile={"goal": "Un modèle"})
    db.save_project(p)
    m1 = Milestone(project_id=p.id, title="Données", position=0)
    m2 = Milestone(project_id=p.id, title="Modèle", position=1)
    db.save_milestone(m1)
    db.save_milestone(m2)
    db.upsert_task(Task(title="[IA] Nettoyer", project_id=p.id, milestone_id=m1.id, status="pending"))
    return db, p, m1, m2


def test_overview_empty_and_with_projects():
    db = Database(":memory:")
    assert "/projet" in project_service.overview(db, NOW)[0]
    db, p, m1, m2 = make_db()
    paused = Project(name="Vieux projet", category="GL", status="paused")
    db.save_project(paused)
    text, buttons = project_service.overview(db, NOW)
    assert "Projet IA" in text and "Étape 1/2" in text and "⏸ En pause : Vieux projet" in text
    assert [b[0][1] for b in buttons] == [f"pjv:{p.id}", f"pjv:{paused.id}"]


def test_detail_buttons_depend_on_status():
    db, p, m1, m2 = make_db()
    text, buttons = project_service.detail(db, p.id, NOW)
    assert "Un modèle" in text and "⏳ Données" in text
    flat = {cb for row in buttons for _, cb in row}
    assert flat == {f"pm:ok:{m1.id}", f"pm:add:{p.id}", f"pp:fix:{p.id}", f"pp:pause:{p.id}", f"pp:end:{p.id}",
                    f"pa:mk:{p.id}"}
    db.save_project(p.model_copy(update={"status": "paused"}))
    assert f"pp:resume:{p.id}" in {cb for row in project_service.detail(db, p.id, NOW)[1] for _, cb in row}
    db.save_project(p.model_copy(update={"status": "done"}))
    assert project_service.detail(db, p.id, NOW)[1] == []
    assert "introuvable" in project_service.detail(db, "zzzzzzzz", NOW)[0]


def test_milestone_ok_is_idempotent_and_announces_next_or_end():
    db, p, m1, m2 = make_db()
    text, buttons = project_service.act(db, "pm", f"ok:{m1.id}", NOW)
    assert "franchie" in text and "Modèle" in text
    assert db.get_milestone(m1.id).status == "done" and db.get_milestone(m1.id).done_at == NOW
    assert db.get_project(p.id).last_activity == NOW
    assert "déjà franchie" in project_service.act(db, "pm", f"ok:{m1.id}", NOW)[0]
    text, buttons = project_service.act(db, "pm", f"ok:{m2.id}", NOW)
    assert "la dernière" in text and buttons[0][0] == ("🏁 Terminer", f"pp:end:{p.id}")
    assert "redemanderai" in project_service.act(db, "pm", f"no:{m1.id}", NOW)[0]


def test_pause_resume_end_are_idempotent_and_end_frees_the_category():
    db, p, m1, m2 = make_db()
    assert "pause" in project_service.act(db, "pp", f"pause:{p.id}", NOW)[0]
    assert db.get_project(p.id).status == "paused"
    assert "Rien n'a changé" in project_service.act(db, "pp", f"pause:{p.id}", NOW)[0]
    assert "repris" in project_service.act(db, "pp", f"resume:{p.id}", NOW)[0]
    assert "terminé" in project_service.act(db, "pp", f"end:{p.id}", NOW)[0]
    assert db.get_project(p.id).status == "done" and db.project_by_category("IA") is None
    assert "terminé" in project_service.act(db, "pp", f"pause:{p.id}", NOW)[0]
    assert db.get_project(p.id).status == "done"
    assert "introuvable" in project_service.act(db, "pp", "end:zzzzzzzz", NOW)[0]


class FakeLLM:
    def __init__(self, *answers):
        self.answers = list(answers)

    def __call__(self, prompt, *, sensitivity, system=None, json_mode=False):
        self.last_prompt, self.last_system = prompt, system
        a = self.answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return json.dumps(a)


class Clock:
    def __call__(self):
        return NOW


def test_add_milestone_flow():
    db, p, m1, m2 = make_db()
    iv = Interview(db, None, "private", {}, Clock())
    assert "Quel est le titre" in iv.begin_add_milestone(p.id)[0][0]
    assert iv.active()
    out = iv.on_text("Soutenance")
    assert "Étape ajoutée" in out[0][0] and "Soutenance" in out[1][0]
    assert [m.title for m in db.list_milestones(p.id)] == ["Données", "Modèle", "Soutenance"]
    assert db.list_milestones(p.id)[2].position == 2 and db.get_draft() is None
    assert "introuvable" in iv.begin_add_milestone("zzzzzzzz")[0][0]
    iv.start()
    assert "déjà en cours" in iv.begin_add_milestone(p.id)[0][0]


def test_fix_project_flow_renames_removes_reorders_and_keeps_statuses():
    db, p, m1, m2 = make_db()
    db.save_milestone(m2.model_copy(update={"status": "done", "done_at": NOW}))   # l'étape conservée est franchie
    sheet = {"name": "Projet IA 2026", "goal": "Un meilleur modèle", "limits": "Sans GPU", "deadline": "2026-12-20",
             "milestones": [{"title": "modèle", "due": None}, {"title": "Évaluation", "due": None}]}
    iv = Interview(db, FakeLLM(sheet), "private", {}, Clock())
    assert "Dis-moi ce qu'il faut changer" in iv.begin_fix_project(p.id)[0][0]
    out = iv.on_text("renomme le projet, retire Données, ajoute Évaluation, change la date")
    assert "Projet IA 2026" in out[-1][0]
    got = db.get_project(p.id)
    assert (got.name, got.deadline, got.profile["goal"], got.profile["limits"]) == (
        "Projet IA 2026", date(2026, 12, 20), "Un meilleur modèle", "Sans GPU")
    assert got.category == "IA"                                     # la catégorie ne change pas par cette voie
    ms = db.list_milestones(p.id)
    assert [m.title for m in ms] == ["Modèle", "Évaluation"] and ms[0].id == m2.id   # étape conservée (même id)
    assert (ms[0].status, ms[0].done_at) == ("done", NOW)
    assert db.get_milestone(m1.id) is None
    assert db.get_task(db.list_project_tasks(p.id)[0].id).milestone_id is None        # tâche détachée
    assert db.get_draft() is None


def test_fix_project_failure_changes_nothing_and_keeps_waiting():
    db, p, m1, m2 = make_db()
    iv = Interview(db, FakeLLM(RuntimeError("down")), "private", {}, Clock())
    iv.begin_fix_project(p.id)
    out = iv.on_text("fais n'importe quoi")
    assert "pas réussi" in out[0][0]
    assert db.get_project(p.id).name == "Projet IA" and len(db.list_milestones(p.id)) == 2
    assert db.get_draft()["stage"] == "fix_project"                   # l'utilisateur peut reformuler ou annuler


def test_fix_project_null_deadline_removes_the_deadline():
    db, p, m1, m2 = make_db()
    sheet = {"name": "Projet IA", "goal": "Un modèle", "deadline": None,
             "milestones": [{"title": "Données", "due": None}, {"title": "Modèle", "due": None}]}
    llm = FakeLLM(sheet)
    iv = Interview(db, llm, "private", {}, Clock())
    iv.begin_fix_project(p.id)
    iv.on_text("plus d'échéance")
    assert '"deadline": "2026-12-15"' in llm.last_prompt      # la fiche actuelle est envoyée à l'IA
    assert db.get_project(p.id).deadline is None
    assert db.get_draft() is None


def test_fix_project_sends_null_for_a_missing_deadline():
    db, p, m1, m2 = make_db()
    db.save_project(p.model_copy(update={"deadline": None}))
    llm = FakeLLM({"name": "Projet IA", "milestones": [{"title": "Données"}, {"title": "Modèle"}]})
    iv = Interview(db, llm, "private", {}, Clock())
    iv.begin_fix_project(p.id)
    iv.on_text("rien de spécial")
    assert '"deadline": null' in llm.last_prompt


def _snapshot(db, pid):
    p = db.get_project(pid)
    return (p.model_dump(), [m.model_dump() for m in db.list_milestones(pid)],
            [(t.id, t.milestone_id) for t in db.list_project_tasks(pid)])


def test_fix_project_partial_answer_changes_only_the_returned_fields():
    db, p, m1, m2 = make_db()
    db.save_milestone(m1.model_copy(update={"status": "done", "done_at": NOW}))
    before_ms = [m.model_dump() for m in db.list_milestones(p.id)]
    before_tasks = [(t.id, t.milestone_id) for t in db.list_project_tasks(p.id)]
    iv = Interview(db, FakeLLM({"name": "X"}), "private", {}, Clock())
    iv.begin_fix_project(p.id)
    out = iv.on_text("renomme en X")
    assert "Projet mis à jour" in out[0][0]
    got = db.get_project(p.id)
    assert got.name == "X" and got.deadline == date(2026, 12, 15) and got.profile == {"goal": "Un modèle"}
    assert [m.model_dump() for m in db.list_milestones(p.id)] == before_ms
    assert [(t.id, t.milestone_id) for t in db.list_project_tasks(p.id)] == before_tasks
    assert db.get_draft() is None


def test_fix_project_explicit_null_text_field_clears_it_and_omitted_deadline_is_kept():
    db, p, m1, m2 = make_db()
    iv = Interview(db, FakeLLM({"goal": None, "limits": "Sans GPU"}), "private", {}, Clock())
    iv.begin_fix_project(p.id)
    iv.on_text("plus d'objectif, limites : sans GPU")
    got = db.get_project(p.id)
    assert got.profile == {"limits": "Sans GPU"} and got.deadline == date(2026, 12, 15) and got.name == "Projet IA"


def test_fix_project_empty_milestone_list_is_a_failure_and_changes_nothing():
    db, p, m1, m2 = make_db()
    before = _snapshot(db, p.id)
    iv = Interview(db, FakeLLM({"name": "X", "milestones": []}), "private", {}, Clock())
    iv.begin_fix_project(p.id)
    out = iv.on_text("supprime tout")
    assert "pas réussi" in out[0][0]
    assert _snapshot(db, p.id) == before
    assert db.get_draft()["stage"] == "fix_project"


def test_fix_project_never_deletes_a_done_milestone_but_deletes_open_omitted_ones():
    db, p, m1, m2 = make_db()
    m3 = Milestone(project_id=p.id, title="Rapport", position=2)
    db.save_milestone(m3)
    db.save_milestone(m1.model_copy(update={"status": "done", "done_at": NOW}))
    iv = Interview(db, FakeLLM({"milestones": [{"title": "Modèle"}]}), "private", {}, Clock())
    iv.begin_fix_project(p.id)
    iv.on_text("garde seulement Modèle")
    ms = db.list_milestones(p.id)
    assert [(m.title, m.id, m.status, m.position) for m in ms] == [
        ("Modèle", m2.id, "open", 0), ("Données", m1.id, "done", 1)]
    assert db.get_milestone(m1.id).done_at == NOW
    assert db.get_milestone(m3.id) is None
    assert db.get_task(db.list_project_tasks(p.id)[0].id).milestone_id == m1.id    # tâche de l'étape conservée


def test_fix_project_duplicate_existing_titles_match_the_first():
    db, p, m1, m2 = make_db()
    dup = Milestone(project_id=p.id, title="modèle", position=2)
    db.save_milestone(dup)
    db.save_milestone(m2.model_copy(update={"status": "done", "done_at": NOW}))
    iv = Interview(db, FakeLLM({"milestones": [{"title": "Données"}, {"title": "Modèle"}]}), "private", {}, Clock())
    iv.begin_fix_project(p.id)
    iv.on_text("rien")
    ms = db.list_milestones(p.id)
    assert [(m.id, m.status) for m in ms] == [(m1.id, "open"), (m2.id, "done")]
    assert db.get_milestone(dup.id) is None


def test_fix_project_truncates_llm_names_to_80_characters():
    db, p, m1, m2 = make_db()
    iv = Interview(db, FakeLLM({"name": "N" * 120, "milestones": [{"title": "Données"}, {"title": "T" * 120}]}),
                   "private", {}, Clock())
    iv.begin_fix_project(p.id)
    iv.on_text("noms longs")
    assert db.get_project(p.id).name == "N" * 80
    assert db.list_milestones(p.id)[1].title == "T" * 80


def test_add_and_fix_on_a_finished_or_missing_project_change_nothing():
    for stage, answer in (("add_milestone", "Soutenance"), ("fix_project", "peu importe")):
        db, p, m1, m2 = make_db()
        iv = Interview(db, FakeLLM({"name": "X"}), "private", {}, Clock())
        iv._save(stage, {"project_id": p.id}, 0, None)
        db.save_project(p.model_copy(update={"status": "done"}))
        before = _snapshot(db, p.id)
        out = iv.on_text(answer)
        assert "terminé, rien n'a changé" in out[0][0]
        assert _snapshot(db, p.id) == before and db.get_draft() is None
        iv._save(stage, {"project_id": "zzzzzzzz"}, 0, None)
        assert "introuvable" in iv.on_text(answer)[0][0] and db.get_draft() is None


def test_new_stages_cancel_and_stale_buttons():
    db, p, m1, m2 = make_db()
    iv = Interview(db, None, "private", {}, Clock())
    for begin, stage in ((iv.begin_add_milestone, "add_milestone"), (iv.begin_fix_project, "fix_project")):
        begin(p.id)
        assert db.get_draft()["stage"] == stage
        for rest in ("ok", "skip", "nodl", "nocat", "cat:IA", "fix"):
            assert "ne correspond plus" in iv.on_callback(rest)[0][0]
        assert db.get_draft()["stage"] == stage                       # rien n'a bougé
        assert "annulé" in iv.on_callback("cancel")[0][0]
        assert db.get_draft() is None


def test_store_routes_actions_and_interviews():
    db, p, m1, m2 = make_db()
    s = CoachScheduler(db, FakeCalendarClient(), load_rules(FROZEN), lambda t, b=None: None, lambda: NOW)
    store.configure(db, s)
    store.configure_projects(FakeLLM())
    assert "Projet IA" in store.projects_overview()[0]
    assert "Un modèle" in store.project_action("pjv", p.id)[0]
    text, buttons = store.project_action("pm", f"add:{p.id}")
    assert "Quel est le titre" in text and store.interview_active()
    store.interview_cancel()
    text, _ = store.project_action("pp", f"fix:{p.id}")
    assert "Dis-moi ce qu'il faut changer" in text
    store.interview_cancel()
    assert "franchie" in store.project_action("pm", f"ok:{m1.id}")[0]


# --- revue finale ---------------------------------------------------------------------------

def test_pas_encore_after_the_last_step_does_not_promise_a_new_question():
    db, p, m1, m2 = make_db()
    text, buttons = project_service.act(db, "pm", "no:-", NOW)
    assert "redemanderai" not in text and "le projet reste ouvert" in text and "/projets" in text and buttons == []


def test_detail_with_80_open_tasks_stays_under_the_telegram_limit_and_keeps_its_buttons():
    db, p, m1, m2 = make_db()
    for i in range(80):
        db.upsert_task(Task(title=f"[IA] tâche ouverte numéro {i} " + "y" * 40, project_id=p.id,
                            milestone_id=m1.id if i < 70 else None, status="pending"))
    text, buttons = project_service.detail(db, p.id, NOW)
    assert len(text) <= 4000
    assert "      … et 66 autres" in text        # 71 tâches ouvertes dans « Données » (70 + [IA] Nettoyer) : 5 montrées
    assert "      … et 5 autres" in text         # 10 sans étape : 5 montrées
    flat = {cb for row in buttons for _, cb in row}
    # 10 tâches sans étape et une étape ouverte : « Ranger les tâches » ; l'étape « Modèle » n'a aucune tâche : « Créer »
    assert flat == {f"pm:ok:{m1.id}", f"pm:add:{p.id}", f"pp:fix:{p.id}", f"pp:pause:{p.id}", f"pp:end:{p.id}",
                    f"pa:rng:{p.id}", f"pa:mk:{p.id}"}


def test_overview_with_40_projects_stays_under_the_telegram_limit():
    db = Database(":memory:")
    for i in range(40):
        p = Project(name=f"Projet numéro {i} " + "z" * 30, category=f"C{i}", profile={"goal": "g"})
        db.save_project(p)
        m = Milestone(project_id=p.id, title="Une étape avec un titre assez long", position=0)
        db.save_milestone(m)
        db.upsert_task(Task(title=f"[C{i}] une tâche à faire avec un titre assez long", project_id=p.id,
                            milestone_id=m.id, status="pending"))
    text, buttons = project_service.overview(db, NOW)
    assert len(text) <= 4000 and text.endswith("… (liste tronquée)")
    assert len(buttons) == 40


def _two_steps_fix(*, done_second=False):
    db, p, m1, m2 = make_db()
    if done_second:
        db.save_milestone(m2.model_copy(update={"status": "done", "done_at": NOW}))
    return db, p, m1, m2


def test_fix_project_sends_step_ids_and_asks_to_keep_them_on_rename():
    db, p, m1, m2 = _two_steps_fix()
    llm = FakeLLM({"milestones": [{"id": m1.id, "title": "Données"}, {"id": m2.id, "title": "Modèle"}]})
    iv = Interview(db, llm, "private", {}, Clock())
    iv.begin_fix_project(p.id)
    iv.on_text("rien")
    assert f'"id": "{m1.id}"' in llm.last_prompt and f'"id": "{m2.id}"' in llm.last_prompt
    assert "garde l'id de toute étape conservée OU RENOMMÉE" in llm.last_system


def test_fix_project_rename_of_an_open_step_with_its_id_keeps_the_step_and_its_tasks():
    db, p, m1, m2 = _two_steps_fix()
    task_id = db.list_project_tasks(p.id)[0].id
    llm = FakeLLM({"milestones": [{"id": m1.id, "title": "Collecte"}, {"id": m2.id, "title": "Modèle"}]})
    iv = Interview(db, llm, "private", {}, Clock())
    iv.begin_fix_project(p.id)
    iv.on_text("renomme Données en Collecte")
    ms = db.list_milestones(p.id)
    assert [(m.id, m.title, m.status) for m in ms] == [(m1.id, "Collecte", "open"), (m2.id, "Modèle", "open")]
    assert db.get_task(task_id).milestone_id == m1.id


def test_fix_project_rename_of_a_done_step_with_its_id_keeps_status_and_date_without_duplicate():
    db, p, m1, m2 = _two_steps_fix(done_second=True)
    llm = FakeLLM({"milestones": [{"id": m1.id, "title": "Données"}, {"id": m2.id, "title": "Modèle final"}]})
    iv = Interview(db, llm, "private", {}, Clock())
    iv.begin_fix_project(p.id)
    iv.on_text("renomme Modèle en Modèle final")
    ms = db.list_milestones(p.id)
    assert [(m.id, m.title, m.status, m.position) for m in ms] == [
        (m1.id, "Données", "open", 0), (m2.id, "Modèle final", "done", 1)]
    assert ms[1].done_at == NOW


def test_fix_project_rename_without_id_still_replaces_open_step_and_keeps_done_one():
    db, p, m1, m2 = _two_steps_fix(done_second=True)
    llm = FakeLLM({"milestones": [{"title": "Collecte"}, {"title": "Modèle final"}]})
    iv = Interview(db, llm, "private", {}, Clock())
    iv.begin_fix_project(p.id)
    iv.on_text("renomme tout")
    ms = db.list_milestones(p.id)
    assert db.get_milestone(m1.id) is None                              # l'étape ouverte non retrouvée est supprimée
    assert db.get_milestone(m2.id).status == "done"                      # la franchie est conservée
    assert sorted((m.title, m.status) for m in ms) == [("Collecte", "open"), ("Modèle", "done"),
                                                        ("Modèle final", "open")]


def test_fix_project_unknown_or_reused_id_falls_back_to_the_title_match():
    db, p, m1, m2 = _two_steps_fix()
    other = Project(name="Autre", category="GL")
    db.save_project(other)
    foreign = Milestone(project_id=other.id, title="Ailleurs", position=0)
    db.save_milestone(foreign)
    llm = FakeLLM({"milestones": [{"id": foreign.id, "title": "Données"}, {"id": m1.id, "title": "Modèle"},
                                  {"id": m1.id, "title": "Autre"}]})
    iv = Interview(db, llm, "private", {}, Clock())
    iv.begin_fix_project(p.id)
    iv.on_text("bizarre")
    assert db.get_milestone(foreign.id).title == "Ailleurs"            # l'étape d'un autre projet n'est pas touchée
    ms = db.list_milestones(p.id)
    titles = [m.title for m in ms]
    assert titles[0] == "Données" and ms[0].id == m1.id                  # id étranger : repli sur le titre
    assert ms[1].id == m2.id and ms[1].title == "Modèle"                 # id déjà utilisé : repli sur le titre
    assert titles[2] == "Autre" and ms[2].id not in (m1.id, m2.id)       # puis nouvelle étape
