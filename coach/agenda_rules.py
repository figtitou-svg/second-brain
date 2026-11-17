"""Règles de l'agenda : cours jamais suivis (masqués, et leurs heures restent libres pour les tâches), créneaux
récurrents que l'utilisateur impose (rugby), et type de chaque créneau pour le colorer.

Titres comparés après nettoyage des codes ADE (« POO2 · CM », « SPE · TD », « Architecture des circuits · TA »).
"""
from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Paris")

# cours jamais suivis : CM, SPE, soutiens TA ; LV1 / LV2 doublonnent « ANG-3 · TD » et « ESP · TD »
HIDDEN = re.compile(r"·\s*(CM|TA)\b|^SPE\b|^LV\s*\d+$", re.I)

# (titre, jour de la semaine 0 = lundi, début, fin) : imposés chaque semaine, bloquent la planification
RECURRING: tuple[tuple[str, int, time, time], ...] = (
    ("Rugby", 2, time(18, 0), time(20, 0)),
    ("Rugby", 3, time(14, 0), time(16, 0)),
)

# type -> couleur (lisible sur fond clair et sombre)
COLORS = {"eval": "#D64545", "td": "#3B82C4", "tp": "#7C5CC4", "lang": "#2A9D8F", "sport": "#E07B24",
          "perso": "#C2477F", "cours": "#55607E"}
LABELS = {"eval": "Évaluation", "td": "TD", "tp": "TP", "lang": "Langue", "sport": "Sport", "perso": "Perso",
          "cours": "Cours"}

_KINDS = (
    ("eval", re.compile(r"évaluation|evaluation|\bDS\b|examen|partiel|\bQCM\b|soutenance", re.I)),
    ("sport", re.compile(r"rugby|\bEPS\b|sport|match|entra[iî]nement", re.I)),
    ("lang", re.compile(r"^(ANG|ESP|ALL|LV)\b|anglais|espagnol", re.I)),
    ("tp", re.compile(r"·\s*TP\b", re.I)),
    ("td", re.compile(r"·\s*TD\b", re.I)),
    ("cours", re.compile(r"·\s*\w+$")),          # autre créneau de l'emploi du temps (« Matière · X »)
)


def is_hidden(title: str) -> bool:
    return bool(HIDDEN.search(title.strip()))


def kind_of(title: str) -> str:
    """Type d'un créneau de l'agenda : évaluation, TD, TP, langue, sport, cours ; sinon perso (rendez-vous)."""
    for kind, rx in _KINDS:
        if rx.search(title):
            return kind
    return "perso"


def color_of(title: str) -> str:
    return COLORS[kind_of(title)]


def recurring_between(start: datetime, end: datetime) -> list[tuple[datetime, datetime, str]]:
    """Occurrences des créneaux récurrents qui chevauchent [start, end[."""
    out = []
    d: date = start.astimezone(TZ).date() - timedelta(days=1)
    last = end.astimezone(TZ).date()
    while d <= last:
        for title, wd, t0, t1 in RECURRING:
            if d.weekday() == wd:
                s, e = datetime.combine(d, t0, tzinfo=TZ), datetime.combine(d, t1, tzinfo=TZ)
                if e > start and s < end:
                    out.append((s, e, title))
        d += timedelta(days=1)
    return out
