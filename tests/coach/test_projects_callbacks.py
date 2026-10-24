"""Spec §8 : tout `callback_data` produit par les écrans de projets doit tenir dans les 64 octets de Telegram,
même avec un identifiant de tâche de 36 caractères (UUID)."""
from __future__ import annotations

import json
from datetime import date

from coach import project_service
from coach.db import Database
from coach.models import Milestone, Project, Task
from coach.project_interview import ORDER, Interview
from tests.coach.conftest import dt

NOW = dt(2026, 10, 20, 10)
TID = "t" * 36
LIMIT = 64
SUBJECTS = {f"Matière numéro {i} avec un nom plutôt long": f"M{i}" for i in range(12)}
SUBJECTS["Japonais"] = "日本語日本語日本語日本語"      # code multi-octets


def _walk(buttons) -> list[str]:
    return [cb for row in buttons for _, cb in row]


def _check(seen: list[str], buttons) -> None:
    seen.extend(_walk(buttons))


def make_db() -> tuple[Database, Project, list[Milestone]]:
    db = Database(":memory:")
    p = Project(name="Un projet au nom vraiment très long pour tester la troncature", category="IA",
                deadline=date(2026, 12, 15), profile={"goal": "Un modèle"}, last_activity=NOW)
    db.save_project(p)
    ms = [Milestone(project_id=p.id, title=f"Étape {i} " + "é" * 60, position=i) for i in range(4)]
    for m in ms:
        db.save_milestone(m)
    for i, m in enumerate(ms):
        db.upsert_task(Task(title=f"[IA] tâche {i}", project_id=p.id, milestone_id=m.id, status="pending"))
    db.upsert_task(Task(id=TID, title="Nettoyer le dataset", status="pending"))
    db.set_proposal(TID, p.id, ms[0].id, "proposed")
    other = Project(name="Autre projet", category="GL")           # sans étape
    db.save_project(other)
    paused = Project(name="Projet en pause", category="BD", status="paused")
    db.save_project(paused)
    return db, p, ms


def test_proposal_card_and_every_pick_path_fit_in_64_bytes():
    seen: list[str] = []
    db, p, ms = make_db()

    card = project_service.proposal_card(db, TID)
    assert card is not None
    _check(seen, card[1])
    assert {cb.split(":")[0] for cb in seen} == {"pjok", "pjwho", "pjno"} and all(TID in cb for cb in seen)

    _, rows = project_service.pick(db, "pjwho", TID, None, NOW)
    _check(seen, rows)
    _, rows = project_service.pick(db, "pjset", TID, p.id, NOW)          # projet avec étapes -> boutons pjms
    _check(seen, rows)
    assert any(cb.startswith("pjms:") for cb in _walk(rows))

    # chaque chemin sur une base neuve (une tâche déjà rattachée ne propose plus rien)
    scenarios = (
        lambda d, p2, ms2: ("pjset", next(x.id for x in d.list_projects("active") if x.category == "GL")),
        lambda d, p2, ms2: ("pjms", "-"),
        lambda d, p2, ms2: ("pjms", ms2[1].id),
        lambda d, p2, ms2: ("pjok", None),
        lambda d, p2, ms2: ("pjno", None),
    )
    for scenario in scenarios:
        db2, p2, ms2 = make_db()
        action, arg = scenario(db2, p2, ms2)
        _, rows = project_service.pick(db2, action, TID, arg, NOW)
        _check(seen, rows)
    assert len(seen) >= 8
    assert not [cb for cb in seen if len(cb.encode()) > LIMIT]


def test_overview_detail_act_and_after_task_done_fit_in_64_bytes():
    seen: list[str] = []
    db, p, ms = make_db()
    other = next(x for x in db.list_projects("active") if x.category == "GL")
    paused = next(x for x in db.list_projects("paused"))

    _check(seen, project_service.overview(db, NOW)[1])
    for project in (p, other, paused):
        _check(seen, project_service.detail(db, project.id, NOW)[1])
    for rest in (f"ok:{ms[0].id}", f"ok:{ms[0].id}", f"no:{ms[0].id}", "no:-", f"pause:{p.id}", f"resume:{p.id}"):
        _check(seen, project_service.act(db, "pm" if rest.split(":")[0] in ("ok", "no") else "pp", rest, NOW)[1])
    _check(seen, project_service.act(db, "pjv", p.id, NOW)[1])
    for m in ms[1:]:                                                     # dernière étape : « Terminer / Pas encore »
        _check(seen, project_service.act(db, "pm", f"ok:{m.id}", NOW)[1])
    _check(seen, project_service.act(db, "pp", f"end:{p.id}", NOW)[1])

    db2, p2, ms2 = make_db()
    t = Task(id="u" * 36, title="[IA] à finir", project_id=p2.id, milestone_id=ms2[0].id, status="done")
    for x in db2.list_project_tasks(p2.id):
        if x.milestone_id == ms2[0].id:
            db2.upsert_task(x.model_copy(update={"status": "done"}))
    db2.upsert_task(t)
    res = project_service.after_task_done(db2, t.id, NOW)
    assert res is not None
    _check(seen, res[1])

    assert {cb.split(":")[0] for cb in seen} >= {"pjv", "pm", "pp"}
    assert not [cb for cb in seen if len(cb.encode()) > LIMIT]


class FakeLLM:
    def __init__(self, *answers):
        self.answers = list(answers)

    def __call__(self, prompt, *, sensitivity, system=None, json_mode=False):
        return json.dumps(self.answers.pop(0))


def test_interview_questions_review_and_create_fit_in_64_bytes():
    seen: list[str] = []
    db, p, ms = make_db()
    for i in range(12):                                                  # beaucoup de codes de catégorie
        db.upsert_task(Task(title=f"[Code{i}] une tâche", status="pending"))
    sheet = {"name": "Nouveau", "goal": "g", "milestones": [{"title": "A", "due": None}]}
    iv = Interview(db, FakeLLM(sheet), "private", SUBJECTS, lambda: NOW)

    def collect(msgs):
        for _, buttons in msgs:
            _check(seen, buttons)

    collect(iv.start())
    for key in ORDER:                                                    # chaque question, y compris le choix de catégorie
        collect([iv._question(key)])
    assert any(cb.startswith("pi:cat:") for cb in seen)
    assert len([cb for cb in seen if cb.startswith("pi:cat:")]) <= 9

    collect(iv.on_text("description du projet"))                         # extraction -> 1re question
    for answer in ("12/01", "mes limites", "mes livrables", "5 h"):
        collect(iv.on_text(answer))
    assert db.get_draft()["stage"] in ("asking", "reviewing")
    while db.get_draft()["stage"] == "asking":
        collect(iv.on_callback("skip"))
    collect([iv._review(db.get_draft()["profile"])])
    collect(iv.on_callback("fix"))
    created = iv.on_callback("ok")
    collect(created)
    assert any(cb.startswith("pjv:") for cb in _walk(created[0][1]))
    collect(iv.begin_add_milestone(p.id))
    iv.cancel()
    collect(iv.begin_fix_project(p.id))
    iv.cancel()

    assert not [cb for cb in seen if len(cb.encode()) > LIMIT]
