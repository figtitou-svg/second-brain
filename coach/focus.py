"""Importance des tâches : classement PUR (aucune E/S), raisons affichées, mise en forme du « focus du jour ».

Le classement est une formule (gratuite, déterministe, expliquable). L'IA ne sert qu'à reformuler la raison
en une phrase (`reasons`), avec une raison de secours calculée si elle ne répond pas.
"""
from __future__ import annotations

import concurrent.futures
import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable

from coach import fmt
from coach.models import Task

log = logging.getLogger("coach.focus")
LLMFn = Callable[..., str]
EXPLAIN_TIMEOUT = 8.0

FOCUS_SIZE = 3
URGENCY_SCALE = 100.0
URGENCY_OVERDUE = 150.0
NO_DUE_MARGIN_DAYS = 14.0
TRAINE_PER_RESCHEDULE = 10
TRAINE_MAX = 30
BOOST_STEP = 25
BOOST_MIN, BOOST_MAX = -1, 2
WEIGHT_MIN, WEIGHT_MAX = 0.5, 2.0
WEIGHT_UP, WEIGHT_DOWN = 1.1, 0.9
ON_DAY_BONUS = 1000
LEVEL_HIGH, LEVEL_MID = 40.0, 15.0
SUBJECT_RE = re.compile(r"^\s*\[([^\]]+)\]")


@dataclass
class Ranked:
    task: Task
    score: float
    rank: int
    level: str  # focus | haut | moyen | faible
    factors: dict[str, Any] = field(default_factory=dict)


def subject_of(title: str) -> str | None:
    """Matière d'une tâche : le préfixe `[Alg]` de son titre."""
    m = SUBJECT_RE.match(title)
    return m.group(1) if m else None


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _same_day(a: datetime, b: datetime) -> bool:
    return a.astimezone(b.tzinfo).date() == b.date()


def _is_on_day(t: Task, now: datetime) -> bool:
    """Tâche à faire un jour précis, et ce jour est aujourd'hui."""
    return bool(t.not_before and t.due and _same_day(t.not_before, now) and _same_day(t.due, now))


def rank_tasks(tasks: list[Task], done_minutes: dict[str, int], now: datetime, weights: dict[str, float],
               day_capacity_minutes: int, min_block_minutes: int = 25) -> list[Ranked]:
    """Classe les tâches (la plus importante d'abord). `now` doit être avec fuseau horaire."""
    scored: list[tuple[float, Task, dict[str, Any]]] = []
    for t in tasks:
        remaining = max(t.estimated_minutes - done_minutes.get(t.id, 0), min_block_minutes)
        need_days = remaining / max(day_capacity_minutes, 1)
        if t.due is None:
            days_left, margin, overdue = None, NO_DUE_MARGIN_DAYS, False
        else:
            days_left = (t.due - now).total_seconds() / 86400
            margin, overdue = days_left - need_days, t.due < now
        urgency = URGENCY_OVERDUE if overdue else URGENCY_SCALE / (1 + max(margin, 0.0))
        traine = min(TRAINE_PER_RESCHEDULE * t.reschedule_count, TRAINE_MAX)
        subject = subject_of(t.title)
        weight = _clamp(weights.get(subject, 1.0) if subject else 1.0, WEIGHT_MIN, WEIGHT_MAX)
        boost = int(_clamp(t.boost, BOOST_MIN, BOOST_MAX))
        on_day = _is_on_day(t, now)
        score = (urgency + traine) * weight + BOOST_STEP * boost + (ON_DAY_BONUS if on_day else 0)
        scored.append((score, t, {"days_left": days_left, "overdue": overdue, "on_day": on_day,
                                  "reschedules": t.reschedule_count, "boost": boost, "weight": weight,
                                  "urgency": urgency, "margin": margin}))
    scored.sort(key=lambda x: (-x[0], x[1].due.timestamp() if x[1].due else float("inf"), x[1].title))
    out: list[Ranked] = []
    for rank, (score, t, factors) in enumerate(scored):
        if rank < FOCUS_SIZE:
            level = "focus"
        elif score >= LEVEL_HIGH:
            level = "haut"
        elif score >= LEVEL_MID:
            level = "moyen"
        else:
            level = "faible"
        out.append(Ranked(t, score, rank, level, factors))
    return out


def adjusted(boost: int, weight: float, up: bool) -> tuple[int, float]:
    """Nouveau (boost, poids de matière) après un clic ⬆ (`up`) ou ⬇, toujours dans les bornes."""
    if up:
        return min(boost + 1, BOOST_MAX), round(min(weight * WEIGHT_UP, WEIGHT_MAX), 3)
    return max(boost - 1, BOOST_MIN), round(max(weight * WEIGHT_DOWN, WEIGHT_MIN), 3)


# --- raison affichée ---------------------------------------------------------------------

_pool = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="focus-llm")

SYSTEM = ("Tu reformules, en UNE phrase courte et naturelle (français, tutoiement, 15 mots maximum), "
          "pourquoi chaque tâche est prioritaire, à partir des faits fournis. N'invente rien. "
          'Réponds UNIQUEMENT avec un JSON de la forme {"id_de_la_tâche": "phrase"}.')


def fallback_reason(r: Ranked) -> str:
    """Raison fabriquée avec les facteurs du score : toujours disponible, sans IA."""
    f, bits = r.factors, []
    if f["on_day"]:
        bits.append("à faire aujourd'hui")
    elif f["overdue"]:
        bits.append("en retard")
    elif f["days_left"] is None:
        bits.append("sans échéance")
    elif f["days_left"] < 1:
        bits.append("rendu dans moins de 24 h")
    else:
        n = max(1, round(f["days_left"]))
        bits.append(f"rendu dans {n} jour{'s' if n > 1 else ''}")
    n = f["reschedules"]
    if n:
        bits.append(f"reportée {n} fois" if n > 1 else "reportée 1 fois")
    if f["boost"] > 0:
        bits.append("tu l'as marquée importante")
    elif f["boost"] < 0:
        bits.append("tu l'as marquée moins importante")
    return " · ".join(bits)


def reasons(ranked: list[Ranked], llm: LLMFn | None = None, db: Any | None = None,
            sensitivity: str = "private", timeout: float = EXPLAIN_TIMEOUT) -> dict[str, str]:
    """id de tâche -> raison. Toujours complet : la phrase de l'IA si elle répond à temps, sinon la raison calculée."""
    base = {r.task.id: fallback_reason(r) for r in ranked}
    if llm is None or not ranked:
        return base
    facts = [(r.task.id, r.task.title, base[r.task.id]) for r in ranked]
    key = "focus:" + hashlib.sha256(json.dumps(facts, ensure_ascii=False).encode()).hexdigest()
    cached = db.cache_get(key) if db is not None else None
    if cached:
        return {**base, **{k: v for k, v in cached[0].items() if k in base}}
    prompt = "Tâches prioritaires :\n" + "\n".join(
        f"- id={i} | titre : {t} | faits : {why}" for i, t, why in facts)
    try:
        raw = _pool.submit(llm, prompt, sensitivity=sensitivity, system=SYSTEM, json_mode=True).result(timeout=timeout)
        data = json.loads(raw[raw.index("{"): raw.rindex("}") + 1])
        phrases = {i: str(v).strip() for i, v in data.items() if i in base and isinstance(v, str) and v.strip()}
    except Exception:  # noqa: BLE001 - quota, timeout, JSON invalide : le focus part quand même
        log.warning("reformulation du focus impossible, raisons de secours", exc_info=True)
        return base
    if phrases and db is not None:
        db.cache_put(key, [phrases])
    return {**base, **phrases}


# --- mise en forme -----------------------------------------------------------------------

def render(ranked: list[Ranked], why: dict[str, str], now: datetime, *, now_mode: bool = False,
           extra_line: str | None = None) -> str:
    """Bloc HTML Telegram : les 3 tâches du focus avec leur raison, puis une ligne pour le reste."""
    if not ranked:
        return "🎯 <b>Rien d'urgent</b>, journée libre 🎉"
    top, rest = ranked[:FOCUS_SIZE], ranked[FOCUS_SIZE:]
    lines = ["🎯 <b>Ton focus</b>"]
    for i, r in enumerate(top, 1):
        mark = "👉 <b>À faire maintenant</b> : " if now_mode and i == 1 else ""
        lines.append(f"{i}. {mark}<b>{fmt.esc(r.task.title)}</b> · {fmt.dur(r.task.estimated_minutes)}\n"
                     f"   <i>{fmt.esc(why.get(r.task.id) or fallback_reason(r))}</i>")
    if extra_line:
        lines.append(extra_line)
    if rest:
        sunday = now.date() + timedelta(days=6 - now.weekday())
        soon = sum(1 for r in rest if r.task.due and r.task.due.astimezone(now.tzinfo).date() <= sunday)
        more = f"+ {len(rest)} autre{'s' if len(rest) > 1 else ''}" + (f", dont {soon} avant dimanche" if soon else "")
        lines.append(f"<i>{more}</i>")
    return "\n".join(lines)


def focus_buttons(ranked: list[Ranked]) -> list[list[tuple[str, str]]]:
    """Une rangée par tâche du focus : ✅ Fait, ⬆ plus important, ⬇ moins important."""
    return [[(f"✅ {i}", f"td:{r.task.id}"), (f"⬆ {i}", f"fup:{r.task.id}"), (f"⬇ {i}", f"fdn:{r.task.id}")]
            for i, r in enumerate(ranked[:FOCUS_SIZE], 1)]
