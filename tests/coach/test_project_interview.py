from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import date, timedelta

from coach import projects
from coach.db import Database
from coach.models import Project, Task
from coach.project_interview import Interview
from tests.coach.conftest import dt

NOW = dt(2026, 10, 20, 10)
SUBJECTS = {"Stage 3IF": "Stg", "Algorithmique avancée pour l'IA et les graphes": "IA"}
SHEET = {"name": "Projet IA", "category": "algorithmique avancée pour l'ia et les graphes",
         "goal": "Entraîner un modèle de classification", "deadline": "2026-12-15", "limits": None,
         "deliverables": None, "team": None, "weekly_hours": None,
         "milestones": [{"title": "Données", "due": None}, {"title": "Modèle", "due": None},
                        {"title": "Rapport", "due": "2026-12-10"}]}


class FakeLLM:
    def __init__(self, *answers):
        self.answers, self.calls = list(answers), []

    def __call__(self, prompt, *, sensitivity, system=None, json_mode=False):
        self.calls.append((prompt, system))
        a = self.answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a if isinstance(a, str) else json.dumps(a)


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def make(*answers, db=None):
    db = db or Database(":memory:")
    llm = FakeLLM(*answers)
    clock = Clock(NOW)
    return Interview(db, llm, "private", SUBJECTS, clock), db, llm, clock


def text_of(msgs):
    return "\n".join(t for t, _ in msgs)


def labels(msg):
    return [label for row in msg[1] for label, _ in row]


def test_description_then_only_missing_questions_then_create():
    iv, db, llm, _ = make(SHEET)
    db.upsert_task(Task(title="[IA] Nettoyer", status="pending"))
    db.upsert_task(Task(title="[IA] Ancienne", status="done"))
    assert "Décris" in text_of(iv.start())
    msgs = iv.on_text("Projet IA : entraîner un modèle, rendu mi-décembre, étapes données, modèle, rapport")
    assert len(llm.calls) == 1 and "limites" in text_of(msgs) and "Passer" in labels(msgs[0])
    msgs = iv.on_text("pas de deep learning")                       # limites
    assert "livrables" in text_of(msgs)
    iv.on_text("un notebook et un rapport")                           # livrables
    msgs = iv.on_text("5 h")                                          # temps par semaine
    assert "seul ou en équipe" in text_of(msgs)
    msgs = iv.on_text("en binôme avec Léa")                           # équipe -> plus rien à demander
    review = text_of(msgs)
    assert "Fiche du projet" in review and "Projet IA" in review and "pas de deep learning" in review
    assert "Rapport" in review and labels(msgs[0]) == ["✅ Créer", "✏️ Corriger", "Annuler"]
    out = iv.on_callback("ok")
    assert "Projet créé" in text_of(out) and "1 tâche" in text_of(out)
    p = db.list_projects()[0]
    assert (p.name, p.category, p.deadline, p.status) == ("Projet IA", "IA", date(2026, 12, 15), "active")
    assert p.profile["limits"] == "pas de deep learning" and p.profile["team"] == "en binôme avec Léa"
    assert [m.title for m in db.list_milestones(p.id)] == ["Données", "Modèle", "Rapport"]
    assert db.list_milestones(p.id)[2].due == date(2026, 12, 10)
    attached = [t for t in db.list_tasks() if t.project_id == p.id]
    assert [t.title for t in attached] == ["[IA] Nettoyer"]            # la tâche terminée n'est pas rattachée
    assert db.get_draft() is None and p.last_activity == NOW


def test_five_questions_cap_and_skip():
    iv, db, llm, _ = make({"name": "Perso", "milestones": []})
    iv.start()
    msgs = iv.on_text("un projet perso")
    asked = [msgs]
    for _ in range(10):
        d = db.get_draft()
        if d["stage"] != "asking":
            break
        asked.append(iv.on_callback("skip"))
    assert db.get_draft()["stage"] == "reviewing"
    assert len(asked) == 6                    # 5 questions (objectif, date, limites, catégorie, livrables), puis la fiche
    assert all("Fiche du projet" not in text_of(a) for a in asked[:5]) and "Fiche du projet" in text_of(asked[5])


def test_no_deadline_button_and_free_date_parsing():
    iv, db, _, _ = make({"name": "P", "goal": "g", "limits": "l", "category": "IA"})
    iv.start()
    iv.on_text("projet")
    assert db.get_draft()["pending"] == "deadline"
    iv.on_text("15 décembre")
    assert db.get_draft()["profile"]["deadline"] == "2026-12-15"

    iv2, db2, _, _ = make({"name": "P", "goal": "g", "limits": "l", "category": "IA"})
    iv2.start()
    iv2.on_text("projet")
    iv2.on_callback("nodl")
    assert db2.get_draft()["profile"]["deadline"] == ""

    iv3, db3, _, _ = make({"name": "P", "goal": "g", "limits": "l", "category": "IA"})
    iv3.start()
    iv3.on_text("projet")
    msgs = iv3.on_text("quand ça sera fini")                          # illisible : une relance
    assert "pas compris" in text_of(msgs) and db3.get_draft()["pending"] == "deadline"
    iv3.on_text("aucune idée de date")                                # toujours illisible : on passe
    assert "deadline" in db3.get_draft()["profile"]["skipped"]


def test_llm_down_falls_back_to_four_fixed_questions():
    iv, db, llm, _ = make(RuntimeError("quota"))
    iv.start()
    msgs = iv.on_text("Un projet de robotique pour le concours")
    assert "s'appelle" in text_of(msgs)                              # nom ; l'objectif = la description
    assert db.get_draft()["profile"]["goal"] == "Un projet de robotique pour le concours"
    msgs = iv.on_text("Robot concours")
    assert "à rendre" in text_of(msgs)
    msgs = iv.on_text("sans échéance")
    assert "catégorie" in text_of(msgs) and "Aucune" in labels(msgs[0])
    msgs = iv.on_callback("nocat")
    assert "Fiche du projet" in text_of(msgs)
    iv.on_callback("ok")
    p = db.list_projects()[0]
    assert (p.name, p.category, p.deadline) == ("Robot concours", None, None) and db.list_milestones(p.id) == []


def test_invalid_json_twice_falls_back():
    iv, db, llm, _ = make("pas du json", "encore pas du json")
    iv.start()
    iv.on_text("description")
    assert len(llm.calls) == 2 and db.get_draft()["profile"]["fallback"] is True


def test_correction_updates_the_sheet_and_failure_keeps_it():
    iv, db, llm, _ = make(SHEET, {**SHEET, "milestones": SHEET["milestones"] + [{"title": "Soutenance", "due": None}]},
                          RuntimeError("down"))
    iv.start()
    iv.on_text("description")
    for answer in ("limites", "livrables", "5 h", "seul"):
        iv.on_text(answer)
    assert db.get_draft()["stage"] == "reviewing"
    assert "Dis-moi ce qu'il faut changer" in text_of(iv.on_callback("fix"))
    msgs = iv.on_text("ajoute une étape Soutenance")
    assert "Soutenance" in text_of(msgs) and db.get_draft()["stage"] == "reviewing"
    iv.on_callback("fix")
    msgs = iv.on_text("autre chose")                                  # l'IA échoue : fiche inchangée
    assert "pas réussi" in text_of(msgs) and "Soutenance" in text_of(msgs)
    iv.on_callback("ok")
    assert [m.title for m in db.list_milestones(db.list_projects()[0].id)][-1] == "Soutenance"


def test_cancel_expiry_and_busy():
    iv, db, _, clock = make()
    assert "Aucun entretien" in text_of(iv.cancel())
    iv.start()
    assert "déjà en cours" in text_of(iv.start())
    assert iv.active()
    clock.t = NOW + timedelta(hours=25)
    assert not iv.active() and db.get_draft() is None                 # expiré, brouillon effacé
    iv.start()
    assert "annulé" in text_of(iv.cancel()) and not iv.active()


def test_state_survives_a_restart(tmp_path):
    path = tmp_path / "coach.sqlite"
    iv, db, _, _ = make(SHEET, db=Database(path))
    iv.start()
    iv.on_text("description")
    db.close()
    iv2, db2, _, _ = make(db=Database(path))
    assert iv2.active() and db2.get_draft()["stage"] == "asking"
    assert "livrables" in text_of(iv2.on_text("pas de deep learning"))


def test_category_already_taken_is_asked_again_without_counting():
    iv, db, _, _ = make(SHEET)
    db.save_project(Project(name="Autre projet IA", category="IA"))
    iv.start()
    iv.on_text("description")
    for answer in ("limites", "livrables", "5 h", "seul"):
        iv.on_text(answer)
    msgs = iv.on_callback("ok")
    out = text_of(msgs)
    assert "appartient déjà" in out and "Autre projet IA" in out
    assert db.get_draft()["stage"] == "asking" and db.get_draft()["pending"] == "category"
    assert not any("IA" == label for label in labels(msgs[-1]))      # la catégorie prise n'est pas proposée
    iv.on_callback("nocat")
    iv.on_callback("ok")
    assert db.list_projects()[-1].name == "Projet IA" and db.list_projects()[-1].category is None


def test_store_configure_resets_the_interview(monkeypatch):
    from coach import store
    from coach.calendar_client import FakeCalendarClient
    from coach.rules import load_rules
    from coach.scheduler import CoachScheduler
    from tests.coach.conftest import FROZEN

    monkeypatch.setattr(store, "_interview_cfg", dict(store._interview_cfg))  # pas de fuite vers les autres tests

    def env():
        db = Database(":memory:")
        return db, CoachScheduler(db, FakeCalendarClient(), load_rules(FROZEN), lambda t, b=None: None, lambda: NOW)

    db1, sched1 = env()
    store.configure(db1, sched1)
    store.configure_projects(FakeLLM(), "private", SUBJECTS)
    assert store._iv().db is db1 and store._iv().subjects == SUBJECTS
    store.interview_start()
    assert db1.get_draft() is not None
    db2, sched2 = env()
    store.configure(db2, sched2)
    assert store._iv().db is db2 and not store.interview_active()   # entretien reconstruit sur la nouvelle base


def test_stale_buttons():
    iv, db, _, _ = make()
    assert "terminé" in text_of(iv.on_callback("ok"))
    iv.start()
    assert "ne correspond plus" in text_of(iv.on_callback("ok"))      # on attend encore la description
    assert "s'appelle" in text_of(iv.on_text("x"))                    # IA sans réponse : repli, on demande le nom
    assert db.get_draft()["stage"] == "asking"


# --- correctifs de la revue ---------------------------------------------------------------
FULL = {"name": "P", "goal": "g", "limits": "l", "category": "IA", "deliverables": "d", "weekly_hours": "w",
        "team": "t", "milestones": [{"title": "Étape", "due": None}]}


def to_review(iv):
    iv.start()
    iv.on_text("description")
    for answer in ("limites", "livrables", "5 h", "seul"):
        iv.on_text(answer)


def test_correction_works_when_there_is_no_deadline():
    iv, db, llm, _ = make(FULL, {**FULL, "name": "P2", "deadline": ""})
    iv.start()
    iv.on_text("projet")
    msgs = iv.on_callback("nodl")                                     # « Sans échéance » -> plus rien à demander
    assert "Fiche du projet" in text_of(msgs) and db.get_draft()["profile"]["deadline"] == ""
    iv.on_callback("fix")
    msgs = iv.on_text("renomme en P2")
    assert '"deadline": null' in llm.calls[-1][0]                     # jamais "" envoyé à l'IA
    assert "pas réussi" not in text_of(msgs) and "P2" in text_of(msgs) and "sans échéance" in text_of(msgs)
    assert db.get_draft()["profile"]["deadline"] == ""


def test_correction_can_change_or_remove_the_deadline():
    iv, db, _, _ = make(SHEET, {**SHEET, "deadline": "2027-01-10"}, {**SHEET, "deadline": None})
    to_review(iv)
    iv.on_callback("fix")
    iv.on_text("repousse au 10 janvier")
    assert db.get_draft()["profile"]["deadline"] == "2027-01-10"
    iv.on_callback("fix")
    msgs = iv.on_text("en fait il n'y a pas d'échéance")
    assert db.get_draft()["profile"]["deadline"] == "" and "sans échéance" in text_of(msgs)


def test_expired_interview_ignores_buttons_and_texts():
    iv, db, _, clock = make(SHEET)
    to_review(iv)
    clock.t = NOW + timedelta(hours=25)
    assert "terminé" in text_of(iv.on_callback("ok"))
    assert db.list_projects() == [] and db.get_draft() is None

    iv2, db2, _, clock2 = make(SHEET)
    iv2.start()
    iv2.on_text("description")
    assert db2.get_draft()["stage"] == "asking"
    clock2.t = NOW + timedelta(hours=25)
    assert "terminé" in text_of(iv2.on_callback("skip")) and db2.get_draft() is None

    iv3, db3, _, clock3 = make(SHEET)
    iv3.start()
    iv3.on_text("description")
    clock3.t = NOW + timedelta(hours=25)
    assert iv3.on_text("pas de deep learning") == [] and db3.get_draft() is None


def test_double_tap_on_create_makes_a_single_project():
    iv, db, _, _ = make(SHEET)
    to_review(iv)
    real_save = db.save_project

    def slow_save(p):                                                  # élargit la fenêtre de concurrence
        time.sleep(0.05)
        real_save(p)

    db.save_project = slow_save
    barrier, results, errors = threading.Barrier(2), [], []

    def tap():
        barrier.wait()
        try:
            results.append(text_of(iv.on_callback("ok")))
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=tap) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == [] and len(db.list_projects()) == 1
    assert sum("Projet créé" in r for r in results) == 1 and sum("terminé" in r for r in results) == 1


def test_category_taken_concurrently_is_asked_again():
    iv, db, _, _ = make(SHEET)
    to_review(iv)
    real_save = db.save_project

    def racing_save(p):                                                # un autre projet prend « IA » juste avant
        db.save_project = real_save
        real_save(Project(name="Concurrent", category="IA"))
        raise sqlite3.IntegrityError("UNIQUE constraint failed: projects.category")

    db.save_project = racing_save
    msgs = iv.on_callback("ok")
    assert "appartient déjà" in text_of(msgs) and "Concurrent" in text_of(msgs)
    assert [p.name for p in db.list_projects()] == ["Concurrent"]
    d = db.get_draft()
    assert d["stage"] == "asking" and d["pending"] == "category" and d["profile"]["category"] is None


def test_create_or_fix_buttons_still_work_while_awaiting_correction():
    iv, db, _, _ = make(SHEET)
    to_review(iv)
    iv.on_callback("fix")
    assert "Dis-moi ce qu'il faut changer" in text_of(iv.on_callback("fix"))
    assert db.get_draft()["stage"] == "awaiting_correction"
    assert "Projet créé" in text_of(iv.on_callback("ok"))
    assert db.list_projects()[0].name == "Projet IA" and db.get_draft() is None


def test_deadline_retry_flag_is_cleared_once_resolved():
    base = {"name": "P", "goal": "g", "limits": "l", "category": "IA"}
    for resolve in (lambda iv: iv.on_text("15 décembre"), lambda iv: iv.on_callback("nodl"),
                    lambda iv: iv.on_callback("skip"), lambda iv: iv.on_text("toujours illisible")):
        iv, db, _, _ = make(base)
        iv.start()
        iv.on_text("projet")
        iv.on_text("quand ça sera fini")
        assert db.get_draft()["profile"]["dl_retry"] is True
        resolve(iv)
        assert "dl_retry" not in db.get_draft()["profile"]


def test_category_button_and_typed_category():
    base = {"name": "P", "goal": "g", "deadline": "2026-12-15", "limits": "l"}
    iv, db, _, _ = make(base)
    iv.start()
    msgs = iv.on_text("projet")
    assert db.get_draft()["pending"] == "category" and "IA" in labels(msgs[0])
    msgs = iv.on_callback("cat:IA")
    assert db.get_draft()["profile"]["category"] == "IA" and "livrables" in text_of(msgs)

    iv2, db2, _, _ = make(base)
    iv2.start()
    iv2.on_text("projet")
    msgs = iv2.on_text("Algorithmique avancée pour l'IA et les graphes")
    assert db2.get_draft()["profile"]["category"] == "IA" and "livrables" in text_of(msgs)
    assert db2.get_draft()["pending"] == "deliverables"


# --- revue finale ---------------------------------------------------------------------------

def _to_review(*answers):
    iv, db, llm, clock = make(SHEET, *answers)
    iv.start()
    iv.on_text("description")
    for answer in ("limites", "livrables", "5 h", "seul"):
        iv.on_text(answer)
    assert db.get_draft()["stage"] == "reviewing"
    return iv, db, llm


def test_draft_correction_with_a_partial_answer_keeps_the_other_fields():
    iv, db, _ = _to_review({"name": "P2"})
    before = db.get_draft()["profile"]
    iv.on_callback("fix")
    msgs = iv.on_text("renomme en P2")
    got = db.get_draft()["profile"]
    assert got["name"] == "P2" and "P2" in text_of(msgs)
    for key in ("goal", "category", "deadline", "limits", "deliverables", "team", "weekly_hours", "milestones"):
        assert got[key] == before[key], key


def test_draft_correction_explicit_null_clears_and_null_deadline_means_no_deadline():
    iv, db, _ = _to_review({"goal": None, "deadline": None, "milestones": [{"title": "Seule", "due": None}]})
    iv.on_callback("fix")
    iv.on_text("enlève l'objectif et la date, une seule étape")
    got = db.get_draft()["profile"]
    assert got["goal"] is None and got["deadline"] == ""
    assert [m["title"] for m in got["milestones"]] == ["Seule"] and got["name"] == "Projet IA"


def test_typed_no_category_or_pass_behaves_like_the_buttons():
    base = {"name": "P", "goal": "g", "deadline": "2026-12-15", "limits": "l"}
    for word in ("aucune", "Aucun", "pas de catégorie", "  Aucune "):
        iv, db, _, _ = make(base)
        iv.start()
        iv.on_text("projet")
        assert db.get_draft()["pending"] == "category"
        msgs = iv.on_text(word)
        prof = db.get_draft()["profile"]
        assert prof.get("category") is None and "category" in prof["skipped"], word
        assert db.get_draft()["pending"] == "deliverables" and "livrables" in text_of(msgs)
    iv, db, _, _ = make(base)
    iv.start()
    iv.on_text("projet")
    iv.on_callback("cat:IA")
    msgs = iv.on_text("Passer")                                      # question « livrables »
    assert "deliverables" in db.get_draft()["profile"]["skipped"]
    assert not db.get_draft()["profile"].get("deliverables") and "Combien de temps" in text_of(msgs)


def test_typed_pass_on_the_deadline_question_skips_it():
    iv, db, _, _ = make({"name": "P", "goal": "g"})
    iv.start()
    iv.on_text("projet")
    assert db.get_draft()["pending"] == "deadline"
    iv.on_text("passer")
    prof = db.get_draft()["profile"]
    assert "deadline" in prof["skipped"] and prof.get("deadline") is None


def test_category_choices_are_limited_by_byte_length():
    db = Database(":memory:")
    iv = Interview(db, None, "private", {"x": "日" * 13, "y": "日" * 15, "z": "é" * 20, "w": "ok"}, Clock(NOW))
    assert set(iv._category_choices()) == {"日" * 13, "ok", "é" * 20}     # 15 × 3 octets = 45 > 40 : écartée
    for c in iv._category_choices():
        assert len(f"pi:cat:{c}".encode()) <= 64


def test_a_long_text_is_not_taken_as_a_category():
    iv, db, _, _ = make(dict(SHEET, category=None, limits="x", deliverables="x"))
    iv.start()
    iv.on_text("Un projet de recherche de job")
    assert db.get_draft()["pending"] == "category"
    out = iv.on_text("Suite de ma description : arriver au mentorat avec des questions précises et répéter à voix haute")
    assert "description" in text_of(out)
    d = db.get_draft()
    assert d["pending"] == "category" and d["profile"]["category"] is None
    iv.on_text("Job")
    assert db.get_draft()["profile"]["category"] == "Job"
