"""Dates en français pour reporter une tâche (« 4 janvier », « janvier », « 15/01 », « lundi »)."""
from __future__ import annotations

import re
import unicodedata
from datetime import date, timedelta

MONTHS = ["janvier", "fevrier", "mars", "avril", "mai", "juin", "juillet", "aout", "septembre", "octobre",
          "novembre", "decembre"]
WEEKDAYS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]


def fold(text: str) -> str:
    """Minuscules sans accents, pour comparer."""
    return "".join(c for c in unicodedata.normalize("NFD", text.lower()) if unicodedata.category(c) != "Mn")


def parse_date(text: str, today: date) -> date | None:
    """Prochaine occurrence future du jour décrit ; un mois seul = son 1er. None si incompréhensible."""
    t = fold(text).strip(" .")
    t = re.sub(r"^(le|la|en|au|a|pour|dans|vers)\s+", "", t)
    t = re.sub(r"^(le|la)\s+", "", t)
    if t in ("demain",):
        return today + timedelta(days=1)
    if t in ("apres-demain", "apres demain"):
        return today + timedelta(days=2)
    if t in ("semaine prochaine", "la semaine prochaine"):
        return today + timedelta(days=7 - today.weekday())
    if t.removeprefix("lundi").strip() == "prochain" and t.startswith("lundi"):
        return today + timedelta(days=7 - today.weekday())
    m = re.fullmatch(r"(\d{1,2})[/.-](\d{1,2})(?:[/.-](\d{2,4}))?", t)
    if m:
        d, mo, y = int(m[1]), int(m[2]), m[3]
        return _build(d, mo, int(y) + (2000 if y and len(y) == 2 else 0) if y else None, today)
    m = re.fullmatch(r"(?:(\d{1,2}|1er)\s+)?([a-z]+)(?:\s+(\d{4}))?", t)
    if m and m[2] in MONTHS:
        d = 1 if not m[1] or m[1] == "1er" else int(m[1])
        return _build(d, MONTHS.index(m[2]) + 1, int(m[3]) if m[3] else None, today)
    if t in WEEKDAYS:
        delta = (WEEKDAYS.index(t) - today.weekday()) % 7 or 7
        return today + timedelta(days=delta)
    return None


def _build(day: int, month: int, year: int | None, today: date) -> date | None:
    try:
        if year is not None:
            return date(year, month, day)
        d = date(today.year, month, day)
        return d if d > today else date(today.year + 1, month, day)
    except ValueError:
        return None
