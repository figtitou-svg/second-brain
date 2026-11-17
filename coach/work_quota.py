"""Heures de travail à faire chaque semaine (job chez Franck, Nomana IT) : le bot les place lui-même.

Chaque semaine (lundi -> dimanche), une tâche « [Job] Franck — 10 h » est créée en priorité haute, à faire
dans la semaine. Le planificateur la découpe en blocs d'1 h à 1 h 30 (QUOTA_BLOCKS) et peut la placer à midi
(le repas n'est pas bloqué pour elle). Créée seulement une fois par semaine : la supprimer ou la terminer ne
la recrée pas.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from coach.models import Task

TZ = ZoneInfo("Europe/Paris")
PREFIX = "quota-"


@dataclass(frozen=True)
class Quota:
    key: str
    title: str
    minutes_per_week: int
    first_week: date          # lundi de la première semaine
    until: date               # dernier jour inclus
    description: str


QUOTAS = (
    Quota("franck", "[Job] Franck (Nomana IT)", 600, date(2026, 11, 16), date(2027, 6, 30),
          "10 h de travail par semaine pour Franck Blettner (Nomana IT). Blocs d'1 h à 1 h 30, placés par le bot."),
)
QUOTA_BLOCKS = (60, 90)      # (bloc minimum, bloc maximum) en minutes
LUNCH_OK = True              # peut être placé pendant le repas du midi


def is_quota(task: Task) -> bool:
    return task.id.startswith(PREFIX)


def block_limits(task: Task) -> tuple[int, int] | None:
    """(min, max) des blocs d'une tâche de quota ; None pour les autres tâches (règles générales)."""
    return QUOTA_BLOCKS if is_quota(task) else None


def skips_blocked(task: Task, label: str) -> bool:
    """Créneau interdit ignoré pour cette tâche (le repas, pour les heures de travail)."""
    return LUNCH_OK and is_quota(task) and label.strip().lower() == "repas"


def monday_of(d: date) -> date:
    return d - timedelta(days=d.weekday())


def week_task(q: Quota, monday: date, now: datetime) -> Task | None:
    """Tâche de la semaine qui commence `monday` (None hors de la période du quota)."""
    if monday < q.first_week or monday > q.until:
        return None
    end = min(monday + timedelta(days=6), q.until)
    start = max(datetime.combine(monday, time(0, 0), tzinfo=TZ), now.astimezone(TZ))
    return Task(id=f"{PREFIX}{q.key}-{monday.isoformat()}",
                title=f"{q.title} — {q.minutes_per_week // 60} h (semaine du {monday.day:02d}/{monday.month:02d})",
                estimated_minutes=q.minutes_per_week, priority="high", not_before=start,
                due=datetime.combine(end, time(23, 59, 59), tzinfo=TZ), source_id=f"quota:{q.key}",
                description=q.description)


def due_tasks(known_ids: set[str], now: datetime) -> list[Task]:
    """Tâches de quota à créer : semaine en cours et semaine suivante, jamais une déjà créée."""
    monday = monday_of(now.astimezone(TZ).date())
    out = []
    for q in QUOTAS:
        for m in (monday, monday + timedelta(days=7)):
            t = week_task(q, m, now)
            if t is not None and t.id not in known_ids:
                out.append(t)
    return out
