"""Planificateur PUR (aucun réseau, aucune E/S) : placement des tâches en blocs.

Algorithme : tâches triées par priorité puis échéance ; pour chacune, on parcourt
les jours depuis `now` jusqu'à l'échéance et on place les blocs au plus tôt dans
les plages autorisées, hors créneaux occupés / bloqués (avec marge `buffer`),
en respectant la durée max d'un bloc et le plafond de focus par jour.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from coach.models import Block, Task
from coach.rules import DAYS, Rules

DEFAULT_DUE_DAYS = 7
_PRIORITY_RANK = {"high": 0, "medium": 1, "low": 2}
_ACTIVE = {"pending", "scheduled"}

Interval = tuple[datetime, datetime]


@dataclass
class PlanResult:
    blocks: list[Block] = field(default_factory=list)
    # task_id -> minutes qui n'ont pas pu être placées avant l'échéance
    unplaced: dict[str, int] = field(default_factory=dict)


def _aware(dt: datetime, tz: ZoneInfo, name: str) -> datetime:
    if dt.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware")
    return dt.astimezone(tz)


def _round_up(dt: datetime, step_min: int = 5) -> datetime:
    """Arrondit au multiple de `step_min` minutes supérieur ou égal."""
    base = dt.replace(minute=0, second=0, microsecond=0)
    elapsed = dt - base
    step = timedelta(minutes=step_min)
    n = -(-elapsed // step)  # plafond
    return base + n * step


def _subtract(window: Interval, cuts: list[Interval]) -> list[Interval]:
    out: list[Interval] = []
    cur = window[0]
    for s, e in sorted(cuts):
        if e <= cur:
            continue
        if s >= window[1]:
            break
        if s > cur:
            out.append((cur, s))
        cur = max(cur, e)
    if cur < window[1]:
        out.append((cur, window[1]))
    return out


def _at(d: date, t: time, tz: ZoneInfo) -> datetime:
    return datetime.combine(d, t, tzinfo=tz)


def plan_detailed(
    tasks: list[Task], busy: list[Interval], rules: Rules, now: datetime,
    already_used: dict[date, int] | None = None, not_before: datetime | None = None,
    not_before_ids: set[str] | None = None,
) -> PlanResult:
    """`already_used` : minutes de focus déjà placées par jour (blocs existants), pour le plafond quotidien.
    `not_before` : rien n'est placé avant (report à demain, à la semaine prochaine) ;
    `not_before_ids` limite cette contrainte à ces tâches (les autres restent placées dès maintenant)."""
    tz = ZoneInfo(rules.timezone)
    now = _aware(now, tz, "now")
    start_from = _round_up(now)
    late_from = start_from
    if not_before is not None:
        late_from = max(start_from, _aware(not_before, tz, "not_before"))
    buf = timedelta(minutes=rules.buffer_minutes)

    cuts: list[Interval] = []
    for s, e in busy:
        s, e = _aware(s, tz, "busy"), _aware(e, tz, "busy")
        if e > s:
            cuts.append((s - buf, e + buf))

    def due_of(t: Task) -> datetime:
        return _aware(t.due, tz, "due") if t.due else now + timedelta(days=DEFAULT_DUE_DAYS)

    base_from = _round_up(now)
    todo = [t for t in tasks if t.status in _ACTIVE]
    todo.sort(key=lambda t: (_PRIORITY_RANK[t.priority], due_of(t), t.title))

    result = PlanResult()
    used: dict[date, int] = dict(already_used or {})
    cap = rules.max_focus_minutes_per_day
    max_b, min_b = rules.max_block_minutes, rules.min_block_minutes

    def lead_start(task: Task, due: datetime) -> datetime | None:
        """Début de la fenêtre de la tâche (« échéance - délai selon sa durée »), None si elle n'en a pas."""
        days = rules.lead_days(task.estimated_minutes) if task.due else None
        return None if days is None else _at(due.date() - timedelta(days=days), time(0, 0), tz)

    def attempt(task: Task, due: datetime, allow_split: bool, use_lead: bool = True) -> int:
        """Place la tâche ; sans `allow_split`, uniquement d'un seul bloc. Retourne les minutes non placées."""
        start_from = late_from if not_before_ids is None or task.id in not_before_ids else base_from
        if task.not_before is not None:  # tâche à faire un jour précis : rien avant ce jour
            start_from = max(start_from, _aware(task.not_before, tz, "not_before"))
        if use_lead and (lead := lead_start(task, due)) is not None:  # pas trop tôt pour sa complexité
            start_from = max(start_from, lead)
        remaining = task.estimated_minutes
        day = start_from.date()
        while remaining > 0 and day <= due.date():
            hours = (
                rules.allowed_hours.weekend if day.weekday() >= 5 else rules.allowed_hours.weekday
            )
            win = (max(_at(day, hours.start, tz), start_from), min(_at(day, hours.end, tz), due))
            day_cuts = list(cuts)
            for b in rules.blocked:
                if DAYS.index(b.day) == day.weekday():
                    day_cuts.append((_at(day, b.from_, tz) - buf, _at(day, b.to, tz) + buf))
            free = _subtract(win, day_cuts) if win[0] < win[1] else []
            for fs, fe in free:
                cursor = fs
                while remaining > 0:
                    left_cap = cap - used.get(day, 0)
                    length = int((fe - cursor).total_seconds() // 60)
                    if not allow_split and min(length, left_cap) < remaining:
                        break  # cette plage est trop courte pour la tâche entière : on regarde la suivante
                    target = min(remaining, max_b)
                    if remaining > max_b and remaining - max_b < min_b:
                        target = remaining - min_b
                    chunk = min(target, length, left_cap)
                    if 0 < remaining - chunk < min_b:  # jamais de reliquat de quelques minutes
                        chunk -= min_b - (remaining - chunk)
                    if chunk <= 0 or (chunk < target and chunk < min_b):
                        break
                    blk = Block(
                        task_id=task.id, start=cursor, end=cursor + timedelta(minutes=chunk)
                    )
                    result.blocks.append(blk)
                    cuts.append((blk.start - buf, blk.end + buf))
                    used[day] = used.get(day, 0) + chunk
                    remaining -= chunk
                    cursor = blk.end + buf
                if remaining <= 0:
                    break
            day += timedelta(days=1)
        return remaining

    def place(task: Task, due: datetime, use_lead: bool) -> int:
        remaining = task.estimated_minutes
        # d'abord un seul bloc (une tâche = une ligne dans l'agenda) ; on ne découpe qu'à défaut
        for allow_split in ((False, True) if task.estimated_minutes <= max_b else (True,)):
            n_cuts, n_blocks, snapshot = len(cuts), len(result.blocks), dict(used)
            remaining = attempt(task, due, allow_split, use_lead)
            if remaining <= 0 or allow_split:
                break
            del cuts[n_cuts:], result.blocks[n_blocks:]  # n'a pas tenu d'un bloc : on annule, puis on découpe
            used.clear()
            used.update(snapshot)
        return remaining

    for task in todo:
        due = due_of(task)
        n_cuts, n_blocks, snapshot = len(cuts), len(result.blocks), dict(used)
        remaining = place(task, due, True)
        if remaining > 0 and lead_start(task, due) is not None:
            # la fenêtre raccourcie ne suffit pas : mieux vaut commencer tôt que ne pas placer la tâche
            del cuts[n_cuts:], result.blocks[n_blocks:]
            used.clear()
            used.update(snapshot)
            remaining = place(task, due, False)
        if remaining > 0:
            result.unplaced[task.id] = remaining

    result.blocks.sort(key=lambda b: b.start)
    return result


def plan(tasks: list[Task], busy: list[Interval], rules: Rules, now: datetime) -> list[Block]:
    """Contrat : retourne les blocs placés (voir `plan_detailed` pour les non-placés)."""
    return plan_detailed(tasks, busy, rules, now).blocks
