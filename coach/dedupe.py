"""Détection des doublons : une même tâche (ou un même rendez-vous) ne doit jamais être créée deux fois,
quelle que soit sa source (mail, note, Notion) ou sa formulation."""
from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from difflib import SequenceMatcher
from typing import Iterable

from coach.models import Event, Task

SIMILARITY = 0.85
_PREFIX = re.compile(r"^\s*\[[^\]]*\]\s*")
_STOP = {"de", "du", "des", "le", "la", "les", "un", "une", "et", "a", "au", "aux", "en", "pour", "sur", "mon", "ma", "mes"}

# « remplir mes informations », « répondre à l'invitation » : des démarches, pas des tâches à planifier
_NOT_A_TASK = re.compile(
    r"\b(compl[eé]ter|remplir|renseigner)\b.*\b(mes|ses|vos|les)?\s*"
    r"(informations?|coordonn[eé]es|donn[eé]es personnelles|infos)\b"
    r"|\b(r[eé]pondre|d[eé]cliner|accepter|refuser)\b.*\binvitation\b",
    re.I,
)


def is_not_a_task(title: str) -> bool:
    return bool(_NOT_A_TASK.search(_strip_accents(title)) or _NOT_A_TASK.search(title))


def _strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")


def norm(title: str) -> str:
    t = _strip_accents(_PREFIX.sub("", title)).lower()
    t = re.sub(r"[^a-z0-9 ]+", " ", t)
    return " ".join(w for w in t.split() if w not in _STOP)


def same_title(a: str, b: str) -> bool:
    na, nb = norm(a), norm(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    ta, tb = set(na.split()), set(nb.split())
    small, big = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    if len(small) >= 3 and small <= big:  # « faire license » ⊂ « faire license ia »
        return True
    return SequenceMatcher(None, na, nb).ratio() >= SIMILARITY


def find_task(task: Task, existing: Iterable[Task]) -> Task | None:
    """Tâche déjà connue (tous statuts : faite ou ignorée, on ne la repropose pas)."""
    for t in existing:
        if t.id == task.id or same_title(t.title, task.title):
            return t
    return None


def find_event(ev: Event, existing: Iterable[Event]) -> Event | None:
    for e in existing:
        if e.id == ev.id or (_same_start(e.start, ev.start) and same_title(e.title, ev.title)):
            return e
    return None


def _same_start(a: datetime, b: datetime) -> bool:
    return abs((a - b).total_seconds()) < 3600  # à l'heure près : le modèle arrondit parfois


def dedupe_tasks(new: Iterable[Task], existing: Iterable[Task]) -> list[Task]:
    """Garde les tâches vraiment nouvelles (ni dans `existing`, ni répétées dans `new`, ni des démarches)."""
    known = list(existing)
    kept: list[Task] = []
    for t in new:
        if is_not_a_task(t.title) or find_task(t, known + kept):
            continue
        kept.append(t)
    return kept
