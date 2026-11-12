"""Infos rangées dans des cases : peu d'infos par mail, pas de doublon, une seule notification par case.

Une info (à savoir, rien à faire) reçoit une case (cours, stages, admin, campus, perso) et une importance (1 à 3).
Par mail : au plus MAX_PER_MAIL infos, celles déjà connues (même sujet) sont écartées, et chaque case n'envoie
qu'un message, pour son info la plus importante, si elle compte assez (NOTIFY_MIN). Les newsletters (expéditeurs
de INFO_SKIP_SENDERS) ne donnent aucune info ; le mail reste en mémoire pour les questions.
"""
from __future__ import annotations

import os
import re
import unicodedata
from difflib import SequenceMatcher

from coach import fmt
from coach.models import Info

CATEGORIES = {"cours": "📚 Cours & examens", "stages": "💼 Stages & jobs", "admin": "🏛️ Administratif",
              "campus": "🎓 Vie INSA", "perso": "👤 Perso"}
DEFAULT_CATEGORY = "perso"
MAX_PER_MAIL = 1          # une seule info par mail, qui résume tout en mots simples
NOTIFY_MIN = 2            # importance 1 (anecdotique) : rangée dans sa case, jamais notifiée
MUTE_AFTER = 2            # infos jugées « pas utiles » d'un expéditeur avant de ne plus en tirer d'infos
DEFAULT_SKIP = ("insa-communication",)   # newsletters de l'école


def category(raw: object) -> str:
    c = str(raw or "").strip().lower()
    return c if c in CATEGORIES else DEFAULT_CATEGORY


def label(cat: str) -> str:
    return CATEGORIES.get(cat, CATEGORIES[DEFAULT_CATEGORY])


def skipped_sender(sender: str | None) -> bool:
    """Expéditeur de newsletters : ses mails ne donnent aucune info (INFO_SKIP_SENDERS, morceaux d'adresse)."""
    if not sender:
        return False
    raw = os.getenv("INFO_SKIP_SENDERS", "").strip()
    parts = [p.strip().lower() for p in raw.split(",") if p.strip()] if raw else list(DEFAULT_SKIP)
    return any(p in sender.lower() for p in parts)


def _norm(text: str) -> str:
    t = unicodedata.normalize("NFKD", text.lower())
    t = "".join(ch for ch in t if not unicodedata.combining(ch))
    return " ".join(re.findall(r"[a-z0-9]+", t))


def similar(a: Info, b: Info) -> bool:
    """Même sujet : titres presque identiques, ou proches et pour le même jour dans la même case."""
    r = SequenceMatcher(None, _norm(a.title), _norm(b.title)).ratio()
    return r >= 0.75 or (r >= 0.5 and a.day is not None and a.day == b.day and a.category == b.category)


def keep(new: list[Info], known: list[Info]) -> list[Info]:
    """Les plus importantes d'abord, au plus MAX_PER_MAIL, sans ce qui est déjà connu (ni répété dans le mail)."""
    out: list[Info] = []
    for i in sorted(new, key=lambda i: -i.importance):
        if len(out) >= MAX_PER_MAIL:
            break
        if not any(similar(i, k) for k in [*known, *out]):
            out.append(i)
    return out


def to_notify(infos: list[Info]) -> list[tuple[Info, int]]:
    """Une notification par case : (son info la plus importante, nombre d'autres infos de la case)."""
    out = []
    for cat in CATEGORIES:
        mine = [i for i in infos if i.category == cat]
        if mine:
            top = max(mine, key=lambda i: i.importance)
            if top.importance >= NOTIFY_MIN:
                out.append((top, len(mine) - 1))
    return out


def message(info: Info, more: int = 0) -> str:
    when = f" · {fmt.day_short(info.day)}" if info.day else ""
    text = f"ℹ️ <b>{label(info.category)}</b>{when} : {fmt.esc(info.title)}"
    if info.detail:
        text += f"\n{fmt.esc(info.detail)}"
    if more:
        text += f"\n<i>+{more} autre{'s' if more > 1 else ''} dans l'onglet Infos</i>"
    return text


def buttons(info_id: str) -> list[list[tuple[str, str]]]:
    return [[("👎 Pas utile", f"inon:{info_id}")]]
