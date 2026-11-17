"""Priorités : l'enjeu de chaque tâche (noté par l'IA), puis une seule tâche à faire par domaine.

Domaine = le projet de la tâche (Security+, Cyber…), sinon sa catégorie (préfixe `[Cha]`, `[Esp]`…), sinon « Divers ».
L'importance d'une tâche = score calculé (échéance comparée au travail restant, reports, retard, ⬆/⬇) multiplié selon
son enjeu (1 à 5, noté par l'IA : examen, note, argent, quelqu'un qui attend…). Chaque domaine est représenté par sa
tâche la plus importante ; les domaines sont triés par cette importance : le premier est « la chose à faire ».
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from coach import focus, projects
from coach.models import Task

log = logging.getLogger("coach.priorities")

STAKES_DEFAULT = 3
STAKES_FACTOR = {1: 0.7, 2: 0.85, 3: 1.0, 4: 1.25, 5: 1.5}
RATE_BATCH = 25                     # tâches notées par appel à l'IA

SYSTEM = (
    "Tu notes l'ENJEU de tâches d'un étudiant ingénieur (INSA Lyon, 3e année informatique, qui a aussi un job "
    "et une association de rugby). Enjeu de 1 à 5 : 5 = conséquence lourde si ce n'est pas fait (examen ou note, "
    "argent, démarche administrative obligatoire, quelqu'un attend après lui, engagement pris) ; 4 = important pour "
    "ses études, son stage ou son job ; 3 = utile ; 2 = secondaire ; 1 = optionnel, confort. Ne tiens PAS compte de "
    "l'échéance (elle est comptée à part). Pour chaque tâche, une raison en 6 mots maximum, en français. "
    'Réponds UNIQUEMENT avec un JSON {"<id>": {"s": <1-5>, "r": "<raison>"}}.'
)


def stakes_key(t: Task) -> str:
    """Change quand ce qui fait l'enjeu change (titre, projet, échéance) : la note est alors refaite."""
    raw = f"{t.title}|{t.project_id or ''}|{t.due.date().isoformat() if t.due else ''}"
    return hashlib.sha256(raw.encode()).hexdigest()[:12]


def valid_stakes(db: Any, tasks: list[Task]) -> dict[str, tuple[int, str | None]]:
    """id -> (enjeu, raison) pour les tâches dont la note est à jour."""
    stored = db.all_stakes()
    out = {}
    for t in tasks:
        s = stored.get(t.id)
        if s and s[2] == stakes_key(t):
            out[t.id] = (s[0], s[1])
    return out


def to_rate(db: Any, tasks: list[Task]) -> list[Task]:
    stored = db.all_stakes()
    return [t for t in tasks if (s := stored.get(t.id)) is None or s[2] != stakes_key(t)]


def rate(db: Any, llm: Callable[..., str], tasks: list[Task], names: dict[str, str],
         now: datetime) -> int:
    """Note jusqu'à RATE_BATCH tâches (titre, catégorie, projet, échéance : jamais le contenu des mails).
    Retourne le nombre de tâches notées ; 0 si l'IA ne répond pas (on réessaiera au prochain passage)."""
    todo = to_rate(db, tasks)[:RATE_BATCH]
    if not todo:
        return 0
    lines = []
    for t in todo:
        cat = focus.subject_of(t.title)
        bits = [f"id={t.id}", f"titre : {t.title}"]
        if t.project_id and t.project_id in names:
            bits.append(f"projet : {names[t.project_id]}")
        elif cat:
            bits.append(f"catégorie : {cat}")
        if t.due:
            bits.append(f"échéance : {t.due.date().isoformat()}")
        lines.append(" | ".join(bits))
    prompt = f"Aujourd'hui : {now.date().isoformat()}\nTâches :\n" + "\n".join(f"- {x}" for x in lines)
    try:
        raw = llm(prompt, system=SYSTEM, json_mode=True)
        data = json.loads(raw[raw.index("{"): raw.rindex("}") + 1])
    except Exception:  # noqa: BLE001 - quota, réseau, JSON invalide : les tâches gardent l'enjeu par défaut
        log.warning("notation de l'enjeu impossible pour l'instant", exc_info=True)
        return 0
    n = 0
    for t in todo:
        v = data.get(t.id)
        if not isinstance(v, dict):
            continue
        try:
            s = min(5, max(1, int(v.get("s"))))
        except (TypeError, ValueError):
            continue
        reason = str(v.get("r") or "").strip()[:80] or None
        db.set_stakes(t.id, s, reason, stakes_key(t))
        n += 1
    return n


def apply_stakes(ranked: list[focus.Ranked], stakes: dict[str, tuple[int, str | None]]) -> list[focus.Ranked]:
    """Scores multipliés par l'enjeu, puis reclassés (rang et niveau recalculés comme dans focus.rank_tasks)."""
    for r in ranked:
        s, why = stakes.get(r.task.id, (STAKES_DEFAULT, None))
        r.score *= STAKES_FACTOR.get(s, 1.0)
        r.factors["stakes"], r.factors["stakes_reason"] = s, why
    ranked.sort(key=lambda r: (-r.score, r.task.due.timestamp() if r.task.due else float("inf"), r.task.title))
    for i, r in enumerate(ranked):
        r.rank = i
        r.level = ("focus" if i < focus.FOCUS_SIZE else "haut" if r.score >= focus.LEVEL_HIGH
                   else "moyen" if r.score >= focus.LEVEL_MID else "faible")
    return ranked


def why(r: focus.Ranked) -> str:
    """Pourquoi cette tâche : l'enjeu noté par l'IA, puis les faits (échéance, reports)."""
    facts = focus.fallback_reason(r)
    reason = r.factors.get("stakes_reason")
    return f"{reason} · {facts}" if reason else facts


@dataclass
class Domain:
    key: str                      # id du projet, « cat:<catégorie> » ou « divers »
    name: str                     # nom du projet, code de catégorie, ou « Divers »
    project_id: str | None
    category: str | None
    top: focus.Ranked
    rest: list[focus.Ranked] = field(default_factory=list)

    @property
    def score(self) -> float:
        return self.top.score


def domains(ranked: list[focus.Ranked], project_names: dict[str, str],
            order: dict[str, tuple] | None = None) -> list[Domain]:
    """Une entrée par domaine ; domaines triés par l'importance de la tâche proposée.
    Hors projet (catégorie, Divers) : la tâche la plus importante. Dans un projet dont `order` donne l'ordre de
    travail (étapes) : la PREMIÈRE tâche non faite, pas l'objectif final (passer l'examen vient en dernier)."""
    order = order or {}
    out: dict[str, Domain] = {}
    for r in ranked:                                     # déjà triées : la première vue d'un domaine est sa tête
        t = r.task
        if t.project_id and t.project_id in project_names:
            key, name, pid, cat = t.project_id, project_names[t.project_id], t.project_id, None
        elif (cat := focus.subject_of(t.title)):
            key, name, pid = f"cat:{projects.fold(cat)}", cat, None
        else:
            key, name, pid, cat = "divers", "Divers", None, None
        if key in out:
            out[key].rest.append(r)
        else:
            out[key] = Domain(key, name, pid, cat, r)
    for d in out.values():
        if d.project_id and any(x.task.id in order for x in [d.top, *d.rest]):
            seq = sorted([d.top, *d.rest], key=lambda x: order.get(x.task.id, (10_001,)))
            d.top, d.rest = seq[0], seq[1:]                # l'étape en cours d'abord, dans l'ordre du programme
    return sorted(out.values(), key=lambda d: -d.score)


_NUM = re.compile(r"\d+")


def _numbers(title: str) -> tuple[int, ...]:
    """Numéros du titre, préfixe [Cat] ôté : « 2.3 · Vulnérabilités (1/2) » -> (2, 3, 1, 2)."""
    m = focus.SUBJECT_RE.match(title)
    return tuple(int(n) for n in _NUM.findall(title[m.end():] if m else title))


def plan_order(tasks: list[Task], positions: dict[str, int]) -> dict[str, tuple]:
    """Ordre dans lequel faire les tâches d'UN projet : étape par étape (position), puis, dans l'étape, l'ordre
    des numéros si toutes les tâches en ont (« 2.3 », « (1/2) », « Machine 6/13 »), sinon la plus courte d'abord.
    Les tâches sans étape passent après les étapes. `positions` : id d'étape -> rang."""
    steps: dict[str | None, list[Task]] = {}
    for t in tasks:
        steps.setdefault(t.milestone_id if t.milestone_id in positions else None, []).append(t)
    out: dict[str, tuple] = {}
    for mid, mine in steps.items():
        pos = positions[mid] if mid is not None else 10_000
        numbered = all(_numbers(t.title) for t in mine)
        for i, t in enumerate(mine):
            inner = (_numbers(t.title), i) if numbered else ((), t.estimated_minutes, i)
            out[t.id] = (pos, inner)
    return out
