"""Le bot apprend des tâches de mails que l'utilisateur ignore : contre-exemples, puis expéditeur mis en sourdine."""
import itertools
import json

import pytest

from coach import extract, store
from coach.calendar_client import FakeCalendarClient
from coach.db import Database
from coach.rules import load_rules
from coach.scheduler import CoachScheduler
from tests.coach.conftest import FROZEN, dt

NOW = dt(2026, 11, 11, 9, 0)
_n = itertools.count()
WORDS = ["Acheter une raquette de badminton", "Réviser la thermodynamique", "Appeler le garagiste pour les pneus",
         "Imprimer le billet de train", "Écrire le compte rendu du TP chimie", "Ranger la cave du sous-sol",
         "Payer la cotisation du club photo", "Commander des lentilles", "Préparer la présentation marketing",
         "Laver la voiture", "Répondre au sondage cantine", "Réserver la salle de répétition",
         "Trier les factures EDF", "Arroser les tomates", "Envoyer la vidéo du gala", "Télécharger le sujet de maths"]


@pytest.fixture
def env(monkeypatch):
    db = Database(":memory:")
    store.configure(db, CoachScheduler(db, FakeCalendarClient(), load_rules(FROZEN), lambda t, b: None, now_fn=lambda: NOW))
    prompts = []

    def llm(prompt, *, sensitivity, system=None, json_mode=False):
        prompts.append(prompt)
        title = WORDS[next(_n) % len(WORDS)]                    # titres très différents : pas de fusion par dedupe
        return json.dumps({"tasks": [{"title": title, "due": None, "estimated_minutes": 30,
                                      "priority": "medium", "confidence": 0.9}], "events": []})

    monkeypatch.setattr(extract, "_resolve_llm", lambda: llm)
    return db, prompts


def mail(sender, k):
    return f"De : Quelqu'un <{sender}>\nObjet : annonce {k} {next(_n)}\n\nTexte {k}"


def test_sender_is_read_from_the_mail_header():
    assert store.mail_sender("De : Prof <Prof.X@insa-lyon.fr>\nObjet : a") == "prof.x@insa-lyon.fr"
    assert store.mail_sender("pas d'en-tête") is None


def test_ignored_mail_tasks_become_counter_examples_then_the_sender_is_muted(env):
    db, prompts = env
    for i in range(store.MUTE_AFTER):
        (t,), _ = store.extract_items(mail("asso@insa-lyon.fr", i), f"insa:mid:{i}-{next(_n)}", "private")
        db.set_task_status(t.id, "dropped")                       # l'utilisateur l'ignore (bouton ou glissement)
    assert "REFUSÉES" in prompts[-1] or store.MUTE_AFTER == 1     # dès le 2e mail, les refus sont dans le prompt
    assert db.sender_verdicts("asso@insa-lyon.fr") == (store.MUTE_AFTER, 0)
    tasks, _ = store.extract_items(mail("asso@insa-lyon.fr", 9), f"insa:mid:x{next(_n)}", "private")
    assert tasks == []                                            # plus proposée
    assert "asso@insa-lyon.fr" in prompts[-1] and "REFUSÉES" in prompts[-1]
    # un autre expéditeur n'est pas concerné
    tasks, _ = store.extract_items(mail("prof@insa-lyon.fr", 1), f"insa:mid:y{next(_n)}", "private")
    assert len(tasks) == 1


def test_a_kept_task_prevents_muting(env):
    db, _ = env
    (kept,), _ = store.extract_items(mail("prof@insa-lyon.fr", 0), f"gmail:k{next(_n)}", "private")
    db.set_task_status(kept.id, "done")
    for i in range(store.MUTE_AFTER):
        (t,), _ = store.extract_items(mail("prof@insa-lyon.fr", i + 1), f"gmail:r{next(_n)}", "private")
        db.set_task_status(t.id, "dropped")
    assert db.sender_verdicts("prof@insa-lyon.fr") == (store.MUTE_AFTER, 1)
    assert len(store.extract_items(mail("prof@insa-lyon.fr", 7), f"gmail:z{next(_n)}", "private")[0]) == 1


def test_notes_are_not_affected(env):
    db, prompts = env
    (t,), _ = store.extract_items(mail("asso@insa-lyon.fr", 0), f"insa:mid:n{next(_n)}", "private")
    db.set_task_status(t.id, "dropped")
    store.extract_items("Acheter du pain", f"note:{next(_n)}", "private")
    assert "REFUSÉES" not in prompts[-1]                          # une note n'est pas un mail : pas de contre-exemples
