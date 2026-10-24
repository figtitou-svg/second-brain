"""Aides partagées par les tests de l'assistant de projet : fausse IA et projet d'exemple."""
from __future__ import annotations

import json
from datetime import date

from coach.db import Database
from coach.models import Milestone, Project
from tests.coach.conftest import dt

NOW = dt(2026, 10, 21, 10)  # mercredi


class FakeLLM:
    """Renvoie les réponses dans l'ordre (dict -> JSON, str tel quel, Exception levée) ; garde les appels."""

    def __init__(self, *answers):
        self.answers, self.calls = list(answers), []

    def __call__(self, prompt, *, sensitivity, system=None, json_mode=False):
        self.calls.append({"prompt": prompt, "sensitivity": sensitivity, "system": system, "json_mode": json_mode})
        a = self.answers.pop(0) if self.answers else {"tasks": []}
        if isinstance(a, Exception):
            raise a
        return a if isinstance(a, str) else json.dumps(a)


def make_project(db=None, **project_fields):
    """Projet « Projet IA » (catégorie IA) avec 3 étapes : Données (faite), Modèle (ouverte, date), Rapport."""
    db = db or Database(":memory:")
    p = Project(name="Projet IA", category="IA", deadline=date(2026, 12, 15), last_activity=NOW,
                profile={"goal": "Entraîner un modèle de classification", "limits": "Pas de deep learning"},
                **project_fields)
    db.save_project(p)
    m1 = Milestone(project_id=p.id, title="Données", position=0, status="done")
    m2 = Milestone(project_id=p.id, title="Modèle", position=1, due=date(2026, 11, 20))
    m3 = Milestone(project_id=p.id, title="Rapport", position=2)
    for m in (m1, m2, m3):
        db.save_milestone(m)
    return db, p, m1, m2, m3


def tasks_json(*items):
    return {"tasks": list(items)}
