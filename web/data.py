# web/data.py
"""Données structurées de la page web (lecture seule) : accueil « coup d'œil »."""
from __future__ import annotations

import hashlib
import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from coach import project_watch, projects
from coach.db import Database

log = logging.getLogger("web.data")

PALETTE = ("#9B8CFF", "#3CC7A6", "#FFA25C", "#5CB8FF", "#F27BC0", "#C7D94B", "#FFD166", "#7FD8E8")
COURSE = "#55607E"      # cours, rendez-vous
NO_PROJECT = "#8FA3C8"  # tâche hors projet

AgendaFn = Callable[[datetime, datetime], list[tuple[datetime, datetime, str]]]


def project_color(project_id: str | None) -> str:
    if not project_id:
        return NO_PROJECT
    return PALETTE[int(hashlib.sha1(project_id.encode()).hexdigest(), 16) % len(PALETTE)]


def project_colors(db: Database) -> dict[str, str]:
    """Une couleur fixe par projet : rang de création parmi TOUS les projets (tout statut), donc distincte pour
    les 8 premiers et inchangée quand un autre projet est terminé ; au-delà, la palette recommence."""
    ranked = sorted(db.list_projects(), key=lambda p: (p.created_at, p.id))
    return {p.id: PALETTE[i % len(PALETTE)] for i, p in enumerate(ranked)}


def _color(colors: dict[str, str], project_id: str | None) -> str:
    if not project_id:
        return NO_PROJECT
    return colors.get(project_id) or project_color(project_id)


class AgendaCache:
    """Agenda Google lu au plus une fois toutes les `ttl` secondes par plage ; panne = dernière valeur connue."""

    def __init__(self, fn: AgendaFn, ttl: float = 300, clock: Callable[[], float] = time.monotonic,
                 retry: float = 60) -> None:
        self.fn, self.ttl, self.clock, self.retry = fn, ttl, clock, retry
        self._cache: dict[tuple[datetime, datetime], tuple[float, list]] = {}   # plage -> (expire à, valeur)
        self._mutex = threading.Lock()

    def get(self, start: datetime, end: datetime) -> list[tuple[datetime, datetime, str]]:
        key = (start, end)
        with self._mutex:
            hit = self._cache.get(key)
        if hit and self.clock() < hit[0]:
            return hit[1]
        try:
            value = list(self.fn(start, end))
            expires = self.clock() + self.ttl
        except Exception:  # noqa: BLE001 - Google en panne : l'accueil s'affiche quand même
            log.warning("agenda Google illisible pour la page web", exc_info=True)
            value = hit[1] if hit else []
            expires = self.clock() + self.retry   # l'échec est mémorisé : pas de nouvel appel à chaque rechargement
        with self._mutex:
            now = self.clock()
            for k in [k for k, (exp, _) in self._cache.items() if k != key and exp <= now]:
                del self._cache[k]   # plages périmées (jours passés) : le dict ne grossit pas indéfiniment
            self._cache[key] = (expires, value)
        return value


@dataclass(frozen=True)
class Segment:
    kind: str        # block | course | event
    title: str
    color: str
    start: datetime
    end: datetime
    left: float      # % depuis le début de la plage
    width: float     # % de la plage


@dataclass(frozen=True)
class NowCard:
    current: bool    # vrai : bloc en cours ; faux : prochain bloc
    title: str
    color: str
    start: datetime
    end: datetime
    minutes_left: int        # en cours : minutes restantes ; sinon minutes avant le début
    total_minutes: int
    block_id: str
    next_title: str | None
    next_start: datetime | None


@dataclass(frozen=True)
class FocusTile:
    rank: int
    title: str
    color: str
    task_id: str


@dataclass(frozen=True)
class Ring:
    project_id: str
    name: str
    color: str
    percent: int
    days_left: int | None
    alert: bool
    alert_line: str | None


@dataclass(frozen=True)
class Counters:
    open_tasks: int
    overdue: int
    suggestions: int


@dataclass(frozen=True)
class Home:
    now: datetime
    day_start: datetime
    day_end: datetime
    now_pct: float
    now_card: NowCard | None
    segments: list[Segment]
    focus: list[FocusTile]
    rings: list[Ring]
    counters: Counters


def day_window(sched, now: datetime) -> tuple[datetime, datetime]:
    hours = sched.rules.allowed_hours.weekend if now.weekday() >= 5 else sched.rules.allowed_hours.weekday
    day = now.date()
    return (datetime.combine(day, hours.start, tzinfo=sched.tz), datetime.combine(day, hours.end, tzinfo=sched.tz))


def _pct(x: datetime, start: datetime, end: datetime) -> float:
    span = (end - start).total_seconds()
    return max(0.0, min(100.0, (x - start).total_seconds() / span * 100))


def _segment(kind: str, title: str, color: str, s: datetime, e: datetime, ws: datetime, we: datetime) -> Segment | None:
    s2, e2 = max(s, ws), min(e, we)
    if e2 <= s2:
        return None
    left = _pct(s2, ws, we)
    return Segment(kind, title, color, s, e, round(left, 2), round(_pct(e2, ws, we) - left, 2))


def build_home(db: Database, sched, agenda: AgendaCache, now: datetime) -> Home:
    ws, we = day_window(sched, now)
    tasks = {t.id: t for t in db.list_tasks()}
    colors = project_colors(db)
    blocks = sorted((b for b in db.list_blocks() if b.outcome in (None, "done") and b.task_id in tasks),
                    key=lambda b: b.start)
    segments: list[Segment] = []
    for b in blocks:
        t = tasks[b.task_id]
        seg = _segment("block", t.title, _color(colors, t.project_id), b.start, b.end, ws, we)
        if seg:
            segments.append(seg)
    for e in db.list_events("added"):
        seg = _segment("event", e.title, COURSE, e.start, e.end, ws, we)
        if seg:
            segments.append(seg)
    for s, e, title in agenda.get(ws, we):
        seg = _segment("course", title, COURSE, s, e, ws, we)
        if seg:
            segments.append(seg)

    # seulement les blocs d'aujourd'hui (commencés avant la fin de la plage du jour) : jamais celui de demain
    upcoming = [b for b in blocks if b.outcome is None and b.end > now and b.start < we]
    now_card = None
    if upcoming:
        cur = upcoming[0]
        nxt = upcoming[1] if len(upcoming) > 1 else None
        t = tasks[cur.task_id]
        current = cur.start <= now
        left = (cur.end - now) if current else (cur.start - now)
        now_card = NowCard(current, t.title, _color(colors, t.project_id), cur.start, cur.end,
                           max(0, int(left.total_seconds() // 60)), int((cur.end - cur.start).total_seconds() // 60),
                           cur.id, tasks[nxt.task_id].title if nxt else None, nxt.start if nxt else None)

    focus = [FocusTile(i, r.task.title, _color(colors, r.task.project_id), r.task.id)
             for i, r in enumerate(sched.ranking(now)[:3], 1)]

    rings: list[Ring] = []
    suggestions = 0
    for p in db.list_projects("active"):
        ms, ptasks = db.list_milestones(p.id), db.list_project_tasks(p.id)
        prog = projects.progress(p, ms, ptasks, now)
        if prog.total_milestones:
            pct = prog.done_milestones * 100 // prog.total_milestones
        else:
            pct = prog.tasks_done * 100 // prog.tasks_total if prog.tasks_total else 0
        stall = project_watch.assess(p, ms, ptasks, now)
        rings.append(Ring(p.id, p.name, _color(colors, p.id), pct, prog.days_left, stall is not None,
                          stall.signals[0].line if stall else None))
        suggestions += sum(1 for s in db.list_project_suggestions(p.id) if s.state == "proposed")

    open_tasks = [t for t in tasks.values() if t.status in projects.OPEN]
    overdue = sum(1 for t in open_tasks if t.due is not None and t.due < now)
    return Home(now, ws, we, round(_pct(now, ws, we), 2), now_card, segments, focus, rings,
                Counters(len(open_tasks), overdue, suggestions))
