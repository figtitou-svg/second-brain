# web/data.py
"""Données structurées de la page web (lecture seule) : accueil « coup d'œil »."""
from __future__ import annotations

import hashlib
import logging
import threading
import time
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from typing import Callable

from coach import focus as coach_focus
from coach import fmt
from coach import project_service, project_watch, projects
from coach.db import Database
from coach.models import Suggestion, Task

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


# --- étape 2 : Agenda et Récap semaine -------------------------------------------------------------------

@dataclass(frozen=True)
class AgendaItem:
    kind: str                 # block | course | event
    title: str
    color: str
    start: datetime
    end: datetime
    top: float                # % de la fenêtre, rogné
    height: float
    lane: int = 0
    lanes: int = 1
    block_id: str | None = None   # seulement pour un bloc ouvert (boutons Fait / Reporter)


@dataclass(frozen=True)
class DueItem:
    title: str
    at: datetime
    color: str
    task_id: str


@dataclass(frozen=True)
class DayView:
    day: date
    window_start: datetime
    window_end: datetime
    items: list[AgendaItem]
    dues: list[DueItem]


def week_start(day: date) -> date:
    return day - timedelta(days=day.weekday())


def _window(sched, day: date) -> tuple[datetime, datetime]:
    hours = sched.rules.allowed_hours.weekend if day.weekday() >= 5 else sched.rules.allowed_hours.weekday
    return (datetime.combine(day, hours.start, tzinfo=sched.tz), datetime.combine(day, hours.end, tzinfo=sched.tz))


def _lanes(items: list[AgendaItem]) -> list[AgendaItem]:
    """Répartit les éléments qui se chevauchent sur des colonnes distinctes (greedy, par groupes de chevauchement)."""
    out: list[AgendaItem] = []
    group: list[tuple[AgendaItem, int]] = []
    group_end: datetime | None = None

    def flush() -> None:
        n = max((lane for _, lane in group), default=-1) + 1
        out.extend(replace(i, lane=lane, lanes=n) for i, lane in group)
        group.clear()

    for it in sorted(items, key=lambda i: (i.start, i.end)):
        if group_end is not None and it.start >= group_end:
            flush()
            group_end = None
        used = {lane for g, lane in group if g.end > it.start}
        lane = next(n for n in range(len(group) + 1) if n not in used)
        group.append((it, lane))
        group_end = max(group_end, it.end) if group_end else it.end
    flush()
    return out


def agenda_day(db: Database, sched, agenda: AgendaCache, day: date) -> DayView:
    ws, we = _window(sched, day)
    colors = project_colors(db)
    tasks = {t.id: t for t in db.list_tasks()}
    items: list[AgendaItem] = []

    def add(kind: str, title: str, color: str, s: datetime, e: datetime, block_id: str | None = None) -> None:
        s2, e2 = max(s, ws), min(e, we)
        if e2 <= s2:
            return
        top = _pct(s2, ws, we)
        items.append(AgendaItem(kind, title, color, s, e, round(top, 2), round(_pct(e2, ws, we) - top, 2),
                                block_id=block_id))

    for b in db.list_blocks():
        t = tasks.get(b.task_id)
        if t is None or b.outcome not in (None, "done"):
            continue
        add("block", t.title, _color(colors, t.project_id), b.start, b.end, b.id if b.outcome is None else None)
    for e in db.list_events("added"):
        add("event", e.title, COURSE, e.start, e.end)
    for s, e, title in agenda.get(ws, we):
        add("course", title, COURSE, s, e)
    dues = [DueItem(t.title, t.due, _color(colors, t.project_id), t.id)
            for t in sorted(tasks.values(), key=lambda t: t.due or ws)
            if t.status in projects.OPEN and t.due is not None and t.due.astimezone(sched.tz).date() == day]
    return DayView(day, ws, we, _lanes(items), dues)


def agenda_week(db: Database, sched, agenda: AgendaCache, monday: date) -> list[DayView]:
    return [agenda_day(db, sched, agenda, monday + timedelta(days=i)) for i in range(7)]


@dataclass(frozen=True)
class SummaryRow:
    name: str
    color: str
    minutes: int
    percent: int              # % de la ligne la plus longue


@dataclass(frozen=True)
class WeekSummary:
    rows: list[SummaryRow]
    total_minutes: int
    worked_tasks: int         # tâches distinctes avec au moins un bloc terminé sur la période
    finished_tasks: int       # parmi elles, celles dont le statut est « done »
    late: list[Task]
    upcoming: list[Task]


def week_summary(db: Database, now: datetime) -> WeekSummary:
    start = now - timedelta(days=7)
    tasks = {t.id: t for t in db.list_tasks()}
    names = {p.id: p.name for p in db.list_projects()}
    colors = project_colors(db)
    minutes: dict[str | None, int] = {}
    worked: set[str] = set()
    for b in db.list_blocks():
        t = tasks.get(b.task_id)
        if b.outcome != "done" or t is None or not (start <= b.end <= now):
            continue
        key = t.project_id if t.project_id in names else None
        minutes[key] = minutes.get(key, 0) + b.minutes
        worked.add(t.id)
    top = max(minutes.values(), default=0)
    rows = [SummaryRow(names.get(k, "Sans projet") if k else "Sans projet", _color(colors, k), m,
                       round(m * 100 / top) if top else 0)
            for k, m in sorted(minutes.items(), key=lambda kv: -kv[1])]
    open_dated = [t for t in tasks.values() if t.status in projects.OPEN and t.due is not None]
    late = sorted((t for t in open_dated if t.due < now), key=lambda t: t.due)
    upcoming = sorted((t for t in open_dated if now <= t.due < now + timedelta(days=7)), key=lambda t: t.due)
    return WeekSummary(rows, sum(minutes.values()), len(worked),
                       sum(1 for i in worked if tasks[i].status == "done"), late, upcoming)


# --- étape 2 : Focus et Tâches ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FocusRow:
    rank: int
    task_id: str
    title: str
    level: str
    score: float
    bar: int                  # % du meilleur score
    reason: str
    due: datetime | None
    minutes: int
    color: str


def focus_rows(sched, now: datetime, limit: int = 30) -> list[FocusRow]:
    ranked = sched.ranking(now)[:limit]
    if not ranked:
        return []
    why = coach_focus.reasons(ranked)                      # raisons calculées : jamais d'appel IA depuis la page web
    colors = project_colors(sched.db)
    best = max(r.score for r in ranked) or 1.0
    return [FocusRow(i, r.task.id, r.task.title, r.level, round(r.score, 1),
                     max(0, min(100, round(r.score * 100 / best))), why.get(r.task.id, ""), r.task.due,
                     r.task.estimated_minutes, _color(colors, r.task.project_id))
            for i, r in enumerate(ranked, 1)]


FILTERS = ("tout", "retard", "semaine", "sans-projet")


@dataclass(frozen=True)
class TaskRow:
    task_id: str
    title: str
    status: str
    due: datetime | None
    overdue: bool
    minutes: int
    step: str | None
    color: str


@dataclass(frozen=True)
class TaskGroup:
    name: str
    color: str
    project_id: str | None
    rows: list[TaskRow]


def task_groups(db: Database, now: datetime, filtre: str = "tout") -> tuple[list[TaskGroup], dict[str, int]]:
    """Tâches ouvertes regroupées par projet (puis « Sans projet »), et le nombre de tâches pour chaque filtre."""
    filtre = filtre if filtre in FILTERS else "tout"
    colors = project_colors(db)
    steps = {m.id: m.title for p in db.list_projects() for m in db.list_milestones(p.id)}
    open_tasks = list(db.list_tasks("pending", "scheduled"))
    names = {p.id: p for p in db.list_projects("active", "paused")}

    def keep(t: Task, f: str) -> bool:
        if f == "retard":
            return t.due is not None and t.due < now
        if f == "semaine":
            return t.due is not None and now <= t.due < now + timedelta(days=7)
        if f == "sans-projet":
            return t.project_id not in names
        return True

    counts = {f: sum(1 for t in open_tasks if keep(t, f)) for f in FILTERS}
    groups: list[TaskGroup] = []
    for p in list(names.values()) + [None]:
        mine = [t for t in open_tasks if keep(t, filtre)
                and ((p is not None and t.project_id == p.id) or (p is None and t.project_id not in names))]
        if not mine:
            continue
        rows = [TaskRow(t.id, t.title, t.status, t.due, t.due is not None and t.due < now, t.estimated_minutes,
                        steps.get(t.milestone_id) if t.milestone_id else None, _color(colors, t.project_id))
                for t in sorted(mine, key=lambda t: (t.due is None, t.due or now, t.title))]
        groups.append(TaskGroup(p.name if p else "Sans projet", _color(colors, p.id) if p else NO_PROJECT,
                                p.id if p else None, rows))
    return groups, counts


# --- étape 2 : Projets -----------------------------------------------------------------------------------

@dataclass(frozen=True)
class ProjectCard:
    project_id: str
    name: str
    color: str
    status: str
    percent: int
    days_left: int | None
    step: str | None
    next_task: str | None
    open_tasks: int
    alert: bool
    alert_line: str | None


def _percent(prog) -> int:
    if prog.total_milestones:
        return prog.done_milestones * 100 // prog.total_milestones
    return prog.tasks_done * 100 // prog.tasks_total if prog.tasks_total else 0


def project_cards(db: Database, now: datetime) -> list[ProjectCard]:
    colors = project_colors(db)
    out: list[ProjectCard] = []
    for p in db.list_projects("active", "paused"):
        ms, tasks = db.list_milestones(p.id), db.list_project_tasks(p.id)
        prog = projects.progress(p, ms, tasks, now)
        stall = project_watch.assess(p, ms, tasks, now)
        out.append(ProjectCard(p.id, p.name, _color(colors, p.id), p.status, _percent(prog), prog.days_left,
                               prog.current.title if prog.current else None,
                               prog.next_task.title if prog.next_task else None,
                               sum(1 for t in tasks if t.status in projects.OPEN), stall is not None,
                               stall.signals[0].line if stall else None))
    return sorted(out, key=lambda c: c.status != "active")


@dataclass(frozen=True)
class StepView:
    step_id: str
    title: str
    status: str
    due: date | None
    tasks: list[Task]
    done: int
    total: int


@dataclass(frozen=True)
class ActionButton:
    label: str
    callback: str
    discuss: bool = False     # la réponse est une suite de conversation : elle s'ouvre dans la Discussion


@dataclass(frozen=True)
class ProjectDetail:
    project_id: str
    name: str
    color: str
    status: str
    percent: int
    days_left: int | None
    deadline: date | None
    facts: list[tuple[str, str]]
    steps: list[StepView]
    loose: list[Task]
    suggestions: list[Suggestion]
    alert_line: str | None
    actions: list[ActionButton]


FACT_LABELS = (("goal", "Objectif"), ("limits", "Limites"), ("deliverables", "Livrables"), ("team", "Équipe"),
               ("weekly_hours", "Temps par semaine"))


def project_detail(db: Database, project_id: str, now: datetime) -> ProjectDetail | None:
    p = db.get_project(project_id)
    if p is None:
        return None
    ms = sorted(db.list_milestones(p.id), key=lambda m: m.position)
    tasks = db.list_project_tasks(p.id)
    prog = projects.progress(p, ms, tasks, now)
    open_tasks = [t for t in tasks if t.status in projects.OPEN]
    step_ids = {m.id for m in ms}
    steps = []
    for m in ms:
        mine = [t for t in tasks if t.milestone_id == m.id and t.status != "dropped"]
        steps.append(StepView(m.id, m.title, m.status, m.due, [t for t in mine if t.status in projects.OPEN],
                              sum(1 for t in mine if t.status == "done"), len(mine)))
    loose = [t for t in open_tasks if not t.milestone_id or t.milestone_id not in step_ids]
    batch = db.pending_batch(p.id)
    suggestions = [s for s in db.list_batch(batch) if s.state == "proposed"] if batch else []
    stall = project_watch.assess(p, ms, tasks, now)
    acts: list[ActionButton] = []
    if p.status != "done":
        if prog.current is not None:
            acts.append(ActionButton("🏁 Étape franchie", f"pm:ok:{prog.current.id}"))
        acts.append(ActionButton("➕ Étape", f"pm:add:{p.id}", True))
        acts.append(ActionButton("✏️ Corriger", f"pp:fix:{p.id}", True))
        if p.status == "active" and prog.current is not None and project_service.rank_candidates(db, p.id):
            acts.append(ActionButton("🗂 Ranger les tâches", f"pa:rng:{p.id}"))
        if p.status == "active" and project_service.step_gaps(db, p.id):
            acts.append(ActionButton("🧩 Créer les tâches des étapes", f"pa:mk:{p.id}"))
        if p.status == "active":
            acts.append(ActionButton("💡 Relancer", f"ps:go:{p.id}"))
        acts.append(ActionButton("⏸ Pause", f"pp:pause:{p.id}") if p.status == "active"
                    else ActionButton("▶ Reprendre", f"pp:resume:{p.id}"))
        acts.append(ActionButton("🏁 Terminer", f"pp:end:{p.id}"))
    return ProjectDetail(p.id, p.name, _color(project_colors(db), p.id), p.status, _percent(prog), prog.days_left,
                         p.deadline, [(label, str(p.profile[k])) for k, label in FACT_LABELS if p.profile.get(k)],
                         steps, loose, suggestions, stall.signals[0].line if stall else None, acts)


# --- étape 2 : ce que lisent les pages ---------------------------------------------------------------------


class Views:
    """Ce que lisent les pages « Organisation » : une méthode par page, le temps injectable pour les tests."""

    def __init__(self, db: Database, sched, agenda_cache: AgendaCache, clock: Callable[[], datetime] | None = None,
                 notes=None) -> None:
        self.db, self.sched, self.agenda_cache = db, sched, agenda_cache
        self._clock = clock
        self.notes_provider = notes     # recent(limit) / search(query, k) / get(doc_id), ou None

    def now(self) -> datetime:
        return (self._clock() if self._clock else self.sched.now()).astimezone(self.sched.tz)

    def agenda(self, day: date | None, vue: str) -> dict:
        now = self.now()
        today = now.date()
        day = day or today
        if vue == "semaine":
            monday = week_start(day)
            return {"vue": "semaine", "day": day, "today": today, "monday": monday,
                    "days": agenda_week(self.db, self.sched, self.agenda_cache, monday)}
        return {"vue": "jour", "day": day, "today": today, "now": now,
                "view": agenda_day(self.db, self.sched, self.agenda_cache, day)}

    def focus(self) -> list[FocusRow]:
        return focus_rows(self.sched, self.now())

    def tasks(self, filtre: str) -> dict:
        now = self.now()
        filtre = filtre if filtre in FILTERS else "tout"
        groups, counts = task_groups(self.db, now, filtre)
        return {"groups": groups, "counts": counts, "filtre": filtre, "now": now}

    def projects(self) -> list[ProjectCard]:
        return project_cards(self.db, self.now())

    def project(self, project_id: str) -> ProjectDetail | None:
        return project_detail(self.db, project_id, self.now())

    def week(self) -> WeekSummary:
        """Échéances ramenées au fuseau du planificateur, comme l'Agenda (une date stockée en UTC change de jour)."""
        w = week_summary(self.db, self.now())
        tz = self.sched.tz

        def local(ts: list[Task]) -> list[Task]:
            return [t.model_copy(update={"due": t.due.astimezone(tz)}) for t in ts]
        return replace(w, late=local(w.late), upcoming=local(w.upcoming))

    # --- étape 3 : À trier, Suggestions, Notifications, Notes, pastilles ---------------------------------

    def a_trier(self) -> dict:
        """Heures ramenées au fuseau du planificateur : le gabarit n'affiche que des heures locales."""
        tz = self.sched.tz

        def local(moment: datetime) -> datetime:
            return moment.replace(tzinfo=tz) if moment.tzinfo is None else moment.astimezone(tz)
        events = [replace(e, start=local(e.start), end=local(e.end)) for e in event_rows(self.db)]
        tasks = [replace(t, due=local(t.due)) if t.due else t for t in unplanned_tasks(self.db, self.now())]
        return {"events": events, "tasks": tasks}

    def suggestions(self) -> dict:
        return suggestions_view(self.db)

    def notifications(self, limit: int = 200) -> list["DayGroup"]:
        return notification_groups(self.db, self.sched.tz, limit, self.now() - timedelta(days=30))

    def notes(self, query: str = "") -> dict:
        q = clean_query(query)
        if self.notes_provider is None:
            return {"query": q, "docs": [], "searched": False}
        raw = self.notes_provider.search(q, 30) if q else self.notes_provider.recent(50)
        return {"query": q, "docs": [self._note(NoteRow, d) for d in raw], "searched": bool(q)}

    def note(self, doc_id: str) -> "NoteFull | None":
        if self.notes_provider is None:
            return None
        d = self.notes_provider.get(doc_id)
        return self._note(NoteFull, d) if d else None

    def _note(self, cls, d: dict):
        at = d.get("created_at")
        if isinstance(at, datetime) and at.tzinfo is None:
            at = at.astimezone(self.sched.tz)      # la mémoire stocke l'heure locale de la VM, sans fuseau
        fields = dict(id=d["id"], title=d.get("title") or "", kind=d.get("kind") or "", sensitivity=d.get("sensitivity") or "",
                      source=d.get("source") or "", created_at=at, folder=d.get("folder"), excerpt=d.get("excerpt") or "")
        if cls is NoteFull:
            fields["body"] = d.get("body") or ""
        return cls(**fields)

    def badges(self) -> dict:
        """Pastilles du menu : quelques requêtes, jamais d'exception (menu vide plutôt qu'une page en erreur)."""
        try:
            now = self.now()
            return {"a_trier": len(self.db.list_events("proposed")) + len(unplanned_tasks(self.db, now)),
                    "suggestions": self.suggestions()["total"],
                    "taches": len(self.db.list_tasks("pending", "scheduled")),
                    "projets_alerte": any(c.alert for c in project_cards(self.db, now))}
        except Exception:  # noqa: BLE001
            log.warning("pastilles du menu illisibles", exc_info=True)
            return {}


class MemoryNotes:
    """Fournisseur de notes de `Views` adossé au module `memory` (lecture seule). `memory` n'est importé qu'à
    l'usage (il charge le routeur LLM) ; toute panne (base absente, verrouillée, corrompue) donne une liste vide
    ou None : une mémoire cassée ne fait jamais tomber une page."""

    def recent(self, limit: int = 50) -> list[dict]:
        try:
            import memory
            return memory.recent_docs(limit)
        except Exception:  # noqa: BLE001
            log.warning("notes : lecture des documents récents impossible", exc_info=True)
            return []

    def search(self, query: str, k: int = 20) -> list[dict]:
        try:
            import memory
            return memory.search_docs(query, k)
        except Exception:  # noqa: BLE001
            log.warning("notes : recherche impossible", exc_info=True)
            return []

    def get(self, doc_id: str) -> dict | None:
        try:
            import memory
            return memory.get_doc(doc_id)
        except Exception:  # noqa: BLE001
            log.warning("notes : lecture d'un document impossible", exc_info=True)
            return None


@dataclass(frozen=True)
class EventRow:
    event_id: str
    title: str
    start: datetime
    end: datetime
    location: str | None


@dataclass(frozen=True)
class BatchItem:
    suggestion_id: str
    title: str
    minutes: int
    step: str | None


@dataclass(frozen=True)
class BatchView:
    project_id: str
    project_name: str
    color: str
    batch_id: str
    items: list[BatchItem]


@dataclass(frozen=True)
class LotRowView:
    task_id: str
    title: str
    step: str


@dataclass(frozen=True)
class LotView:
    project_id: str
    project_name: str
    color: str
    rows: list[LotRowView]


@dataclass(frozen=True)
class ProposalView:
    task_id: str
    task_title: str
    text: str
    buttons: list


@dataclass(frozen=True)
class NotificationRow:
    at: datetime
    text: str
    buttons: list
    delivered: bool


@dataclass(frozen=True)
class DayGroup:
    label: str
    items: list[NotificationRow]


@dataclass(frozen=True)
class NoteRow:
    id: str
    title: str
    kind: str
    sensitivity: str
    source: str
    created_at: datetime | None
    folder: str | None
    excerpt: str


@dataclass(frozen=True)
class NoteFull(NoteRow):
    body: str = ""


def clean_query(query: str | None) -> str:
    q = "".join(c if c.isprintable() else " " for c in (query or ""))
    return " ".join(q.split())[:200]


def event_rows(db: Database) -> list[EventRow]:
    return [EventRow(e.id, e.title, e.start, e.end, e.location) for e in db.list_events("proposed")]


def unplanned_tasks(db: Database, now: datetime) -> list[TaskRow]:
    """Tâches `pending` sans bloc ouvert, par échéance (sans échéance en dernier)."""
    placed = {b.task_id for b in db.list_blocks(open_only=True)}
    todo = [t for t in db.list_tasks("pending") if t.id not in placed]
    colors = project_colors(db)
    steps = {m.id: m.title for p in db.list_projects() for m in db.list_milestones(p.id)}
    return [TaskRow(t.id, t.title, t.status, t.due, t.due is not None and t.due < now, t.estimated_minutes,
                    steps.get(t.milestone_id) if t.milestone_id else None, _color(colors, t.project_id))
            for t in sorted(todo, key=lambda t: (t.due is None, t.due or now, t.title))]


def suggestions_view(db: Database) -> dict:
    colors = project_colors(db)
    batches: list[BatchView] = []
    lots: list[LotView] = []
    for p in db.list_projects("active"):
        steps = {m.id: m.title for m in db.list_milestones(p.id)}
        batch = db.pending_batch(p.id)
        items = [BatchItem(s.id, s.title, s.estimated_minutes, steps.get(s.milestone_id) if s.milestone_id else None)
                 for s in (db.list_batch(batch) if batch else []) if s.state == "proposed"]
        if items:
            batches.append(BatchView(p.id, p.name, _color(colors, p.id), batch, items))
        rows = []
        for r in db.list_lot_proposals(p.id):
            task = db.get_task(r["task_id"])
            if r["state"] == "lot" and task is not None and r["milestone_id"] in steps:
                rows.append(LotRowView(task.id, task.title, steps[r["milestone_id"]]))
        if rows:
            lots.append(LotView(p.id, p.name, _color(colors, p.id), rows))
    proposals: list[ProposalView] = []
    for prop in db.list_proposals("proposed"):
        card = project_service.proposal_card(db, prop["task_id"])
        task = db.get_task(prop["task_id"])
        if card is not None and task is not None:
            proposals.append(ProposalView(task.id, task.title, card[0], card[1]))
    total = sum(len(b.items) for b in batches) + sum(len(l.rows) for l in lots) + len(proposals)
    return {"batches": batches, "lots": lots, "proposals": proposals, "total": total}


def notification_groups(db: Database, tz, limit: int = 200, since: datetime | None = None) -> list[DayGroup]:
    groups: list[DayGroup] = []
    last: date | None = None
    for n in db.list_notifications(limit, since):
        at = n.sent_at.astimezone(tz)
        if at.date() != last:
            groups.append(DayGroup(fmt.day_long(at), []))
            last = at.date()
        groups[-1].items.append(NotificationRow(at, n.text, n.buttons, n.delivered))
    return groups
