"""/rdv (ou /event) : forcer la création d'un rendez-vous à partir d'un texte (ou de ce qu'on lit sur une photo).

L'IA rend titre, date, heures et lieu ; sans heure claire, le rendez-vous attend (statut « draft ») et le bot
demande l'heure (boutons ou réponse « 16h30 »). Jamais de tâche ni d'info : c'est un rendez-vous demandé.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Callable
from zoneinfo import ZoneInfo

from coach.models import Event

TZ = ZoneInfo("Europe/Paris")
DEFAULT_MINUTES = 60
HOUR_CHOICES = ("08:00", "10:00", "12:00", "14:00", "16:00", "18:00", "20:00")
HOUR_RE = re.compile(r"^\s*(?:à\s*)?(\d{1,2})\s*(?:h|:|H)\s*(\d{2})?\s*$")

SYSTEM = (
    "Tu lis la description d'UN rendez-vous (texte, ou ce qu'on lit sur une photo : affiche, convocation, capture "
    "d'écran) et tu en tires ses informations. Réponds UNIQUEMENT avec un JSON "
    '{"title":str,"date":"AAAA-MM-JJ"|null,"start":"HH:MM"|null,"end":"HH:MM"|null,"location":str|null}. '
    "title : court et en français (« Dentiste », « Match de rugby contre Lyon 2 »). date : utilise le calendrier "
    "fourni (ne calcule pas les jours de la semaine). start / end : seulement si l'heure est écrite ; sinon null "
    "(n'invente jamais d'heure). location : le lieu s'il est donné."
)


@dataclass
class Parsed:
    title: str
    day: date | None
    start: time | None
    end: time | None
    location: str | None


def _hm(raw: object) -> time | None:
    m = re.match(r"^\s*(\d{1,2})\s*[:hH]\s*(\d{2})?", str(raw or ""))
    if not m or int(m[1]) > 23 or int(m[2] or 0) > 59:
        return None
    return time(int(m[1]), int(m[2] or 0))


def parse(text: str, llm: Callable[..., str], now: datetime) -> Parsed | None:
    """None si l'IA ne répond pas ou ne voit aucun rendez-vous."""
    jours = ("lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche")
    cal = "\n".join(f"{jours[d.weekday()]} {d.date().isoformat()}" for d in (now + timedelta(days=i) for i in range(30)))
    prompt = f"Date actuelle : {jours[now.weekday()]} {now.date().isoformat()}\nCalendrier :\n{cal}\n\nRendez-vous :\n{text}"
    try:
        raw = llm(prompt, system=SYSTEM, json_mode=True)
        data = json.loads(raw[raw.index("{"): raw.rindex("}") + 1])
    except Exception:  # noqa: BLE001 - quota, réseau, JSON invalide
        return None
    title = str(data.get("title") or "").strip()
    if not title:
        return None
    try:
        day = date.fromisoformat(str(data.get("date"))) if data.get("date") else None
    except ValueError:
        day = None
    return Parsed(title[:120], day, _hm(data.get("start")), _hm(data.get("end")),
                  (str(data.get("location")).strip() or None) if data.get("location") else None)


def to_event(p: Parsed, source_id: str) -> Event:
    """Rendez-vous (heure de début connue ou provisoire à 9h si on va la demander)."""
    start = datetime.combine(p.day, p.start or time(9, 0), tzinfo=TZ)
    end = datetime.combine(p.day, p.end, tzinfo=TZ) if p.end else start + timedelta(minutes=DEFAULT_MINUTES)
    if end <= start:
        end = start + timedelta(minutes=DEFAULT_MINUTES)
    eid = hashlib.sha1(f"{source_id}|{p.title.lower()}|{p.day}".encode()).hexdigest()[:16]
    return Event(id=eid, title=p.title, start=start, end=end, location=p.location, source_id=source_id)


def at_hour(ev: Event, hh: int, mm: int) -> Event:
    """Le rendez-vous à l'heure choisie, même durée."""
    dur = ev.end - ev.start
    start = datetime.combine(ev.start.astimezone(TZ).date(), time(hh, mm), tzinfo=TZ)
    return ev.model_copy(update={"start": start, "end": start + dur})


def hour_buttons(event_id: str) -> list[list[tuple[str, str]]]:
    row = [(h.replace(":00", "h"), f"rdvh:{event_id}:{h.replace(':', '')}") for h in HOUR_CHOICES]
    return [row[:4], row[4:] + [("🗑 Annuler", f"evign:{event_id}")]]
