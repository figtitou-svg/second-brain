"""Mise en forme des messages Telegram (HTML) : dates en français, durées, fiches lisibles."""
from __future__ import annotations

import html
import re
from datetime import datetime
from typing import Any

JOURS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
MOIS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre",
        "octobre", "novembre", "décembre"]
MOIS_COURT = ["janv.", "févr.", "mars", "avr.", "mai", "juin", "juil.", "août", "sept.", "oct.", "nov.", "déc."]
PRIO = {"high": "🔴 haute", "medium": "🟠 moyenne", "low": "🟢 basse"}
STATUS_ICON = {"pending": "⏳", "scheduled": "📌", "done": "✅", "dropped": "🗑"}


def esc(s: Any) -> str:
    return html.escape(str(s), quote=False)


def day_short(dt: datetime) -> str:
    return f"{JOURS[dt.weekday()][:3]}. {dt.day} {MOIS_COURT[dt.month - 1]}"


def day_long(dt: datetime) -> str:
    return f"{JOURS[dt.weekday()]} {dt.day} {MOIS[dt.month - 1]}"


def hm(dt: datetime) -> str:
    return f"{dt:%H:%M}"


def rng(s: datetime, e: datetime) -> str:
    return f"{hm(s)}–{hm(e)}"


def when(dt: datetime) -> str:
    return f"{day_short(dt)} {hm(dt)}"


def dur(minutes: int) -> str:
    h, m = divmod(int(minutes), 60)
    if h and m:
        return f"{h} h {m:02d}"
    return f"{h} h" if h else f"{m} min"


def md_to_html(text: str) -> str:
    """Réponse d'un LLM (Markdown léger) -> HTML Telegram sûr."""
    out = esc(text)
    out = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", out)
    out = re.sub(r"^\s*[*-] ", "• ", out, flags=re.M)
    return out


def on_day(t: Any) -> bool:
    """Tâche à faire un jour précis (et non avant une échéance)."""
    nb, due = getattr(t, "not_before", None), getattr(t, "due", None)
    return bool(nb and due and nb.date() == due.date())


def task_card(t: Any, header: str = "🆕 <b>Nouvelle tâche</b>") -> str:
    bits = [f"⏱ {dur(t.estimated_minutes)}"]
    if on_day(t):
        bits.append(f"📆 le {day_short(t.not_before)}")
    elif getattr(t, "due", None):
        bits.append(f"📆 avant {when(t.due)}")
    if getattr(t, "priority", None) in PRIO:
        bits.append(PRIO[t.priority])
    return f"{header}\n<b>{esc(t.title)}</b>\n{' · '.join(bits)}"


def event_card(e: Any, header: str) -> str:
    lines = [header, f"<b>{esc(e.title)}</b>", f"{day_short(e.start)} · {rng(e.start, e.end)}"]
    if getattr(e, "location", None):
        lines.append(f"📍 {esc(e.location)}")
    return "\n".join(lines)
