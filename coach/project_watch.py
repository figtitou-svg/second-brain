"""Surveillance des projets (B2) : repère un projet actif qui stagne ou dont l'échéance est en danger.

`assess` est PUR (aucune E/S, aucune IA) : il décide si un projet est en difficulté et pourquoi. Le job `watch`
(plus bas) envoie au plus un message par jour, avec un anti-spam fondé sur les clés `notifications`.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Callable

from coach import fmt, projects
from coach.db import Database
from coach.models import Milestone, Project, Task

log = logging.getLogger("coach.project_watch")

STALL_DAYS = 7          # sans activité depuis ce nombre de jours : le projet est « inactif »
HORIZON_DAYS = 30       # le rendu n'est « en danger » que s'il tombe dans cet horizon
DAILY_MINUTES = 90      # travail qu'on suppose faisable par jour sur un projet
DEFAULT_MINUTES = 30    # durée d'une tâche sans estimation
MAX_MINUTES = 480       # plafond par tâche : une durée aberrante ne déclenche pas une fausse alerte
TITLE_MAX = 80
RE_ALERT_DAYS = 4       # une même situation n'est pas re-signalée avant ce nombre de jours
MAX_ALERTS = 3          # alertes au plus par situation
RANK = {"overdue": 0, "late_step": 1, "at_risk": 2, "inactive": 3}   # du plus grave au moins grave


@dataclass(frozen=True)
class Signal:
    code: str    # clé de RANK
    line: str    # ligne du message (HTML déjà échappé)


@dataclass(frozen=True)
class Stall:
    project_id: str
    name: str
    signals: tuple[Signal, ...]   # dans l'ordre de gravité, jamais vide
    last_date: date               # date de dernière activité effective

    @property
    def severity(self) -> str:
        return self.signals[0].code

    @property
    def situation(self) -> str:
        """Identifie « le même problème » : dès que le projet bouge ou s'aggrave, la situation change."""
        return f"{self.last_date.isoformat()}:{self.severity}"


def _cut(text: str) -> str:
    return " ".join(text.split())[:TITLE_MAX]


def _last_date(project: Project, now: datetime) -> date:
    """Jour de la dernière activité : `last_activity`, sinon la création du projet ; une date naïve est lue à l'heure locale."""
    last = project.last_activity or project.created_at
    if last.tzinfo is None:
        last = last.replace(tzinfo=now.tzinfo)
    return last.astimezone(now.tzinfo).date()


def assess(project: Project, milestones: list[Milestone], tasks: list[Task], now: datetime) -> Stall | None:
    """Les signaux vrais pour ce projet (du plus grave au moins grave), ou None s'il va bien ou n'est pas actif."""
    if project.status != "active":
        return None
    today = now.date()
    signals: list[Signal] = []
    open_tasks = [t for t in tasks if t.status in projects.OPEN]
    deadline = project.deadline
    if deadline is not None and deadline < today:
        signals.append(Signal("overdue", "la date de rendu est dépassée"))
    current = next((m for m in sorted(milestones, key=lambda m: m.position) if m.status == "open"), None)
    if current is not None and current.due is not None and current.due < today:
        signals.append(Signal("late_step", f"l'étape <i>{fmt.esc(_cut(current.title))}</i> "
                                           f"devait finir le {current.due:%d/%m}"))
    if deadline is not None and open_tasks:
        left = (deadline - today).days
        remaining = sum(min(t.estimated_minutes or DEFAULT_MINUTES, MAX_MINUTES) for t in open_tasks)
        if 0 <= left <= HORIZON_DAYS and (left <= 1 or remaining > left * DAILY_MINUTES):
            when = "aujourd'hui" if left == 0 else f"{left} jour{'s' if left > 1 else ''}"
            signals.append(Signal("at_risk", f"il reste {fmt.dur(remaining)} de travail pour {when}"))
    last = _last_date(project, now)
    idle = (today - last).days
    if idle >= STALL_DAYS:
        signals.append(Signal("inactive", f"rien n'a bougé depuis {idle} jours"))
    if not signals:
        return None
    return Stall(project.id, project.name, tuple(signals), last)   # déjà dans l'ordre de RANK


def alert_text(stall: Stall) -> str:
    """Message d'alerte : le projet puis une ligne par signal."""
    return "\n".join([f"⚠️ <b>{fmt.esc(_cut(stall.name))}</b> :"] + [f"• {s.line}" for s in stall.signals])


def alert_buttons(project_id: str) -> list[list[tuple[str, str]]]:
    return [[("⏸ Pause", f"pp:pause:{project_id}"), ("💡 Relancer", f"ps:go:{project_id}"),
             ("📂 Voir le projet", f"pjv:{project_id}")]]


# --- le job -------------------------------------------------------------------------------

def _spaced(db: Database, project_id: str, severity: str, today: date) -> bool:
    """Vrai si ce niveau de gravité n'a pas été signalé aujourd'hui ni dans les RE_ALERT_DAYS - 1 jours précédents.
    L'espacement ignore l'activité (un projet qu'on touche chaque jour ne doit pas être relancé chaque jour) ;
    un problème plus grave, lui, passe sans attendre."""
    return not any(db.has_notification(f"pwalert:{project_id}:{severity}:{(today - timedelta(days=n)).isoformat()}")
                   for n in range(RE_ALERT_DAYS))


def _free_slot(db: Database, project_id: str, situation: str) -> int | None:
    """Premier numéro d'alerte libre (1 à MAX_ALERTS) pour cette situation ; None : plafond atteint."""
    return next((n for n in range(1, MAX_ALERTS + 1)
                 if not db.has_notification(f"pwslot:{project_id}:{situation}:{n}")), None)


def _candidates(db: Database, now: datetime) -> list[tuple[Stall, int]]:
    """Projets à signaler aujourd'hui avec leur numéro d'alerte, le plus grave d'abord, puis le plus ancien."""
    today = now.date()
    found: list[tuple[Stall, int]] = []
    for p in db.list_projects("active"):
        try:
            tasks = db.list_project_tasks(p.id)
            stall = assess(p, db.list_milestones(p.id), tasks, now)
            if stall is None or db.pending_batch(p.id):
                continue   # va bien, ou des suggestions attendent déjà un clic
            if stall.severity == "inactive" and not any(t.status in projects.OPEN for t in tasks):
                continue   # inactif et sans tâche ouverte : c'est le travail de l'assistant de projet (B1)
            if not _spaced(db, p.id, stall.severity, today):
                continue
            slot = _free_slot(db, p.id, stall.situation)
            if slot is not None:
                found.append((stall, slot))
        except Exception:  # noqa: BLE001 - un projet illisible ne doit pas empêcher les autres
            log.warning("surveillance des projets : projet %s ignoré", p.id, exc_info=True)
    found.sort(key=lambda c: (RANK[c[0].severity], c[0].last_date))
    return found


def watch(db: Database, notify: Callable[..., Any], now: datetime) -> int:
    """Envoie au plus UNE alerte par jour (tous projets). Renvoie le nombre de messages envoyés. Ne lève jamais.
    Les clés ne sont posées qu'après un envoi réussi : si Telegram échoue, on retentera au passage suivant."""
    day = now.date().isoformat()
    try:
        if db.has_notification(f"pwatch:day:{day}"):
            return 0
        todo = _candidates(db, now)
    except Exception:  # noqa: BLE001
        log.warning("surveillance des projets : passage impossible", exc_info=True)
        return 0
    for stall, slot in todo:
        try:
            delivered = notify(alert_text(stall), alert_buttons(stall.project_id)) is not False   # None : réputé envoyé
        except Exception:  # noqa: BLE001
            log.warning("surveillance des projets : envoi impossible pour %s", stall.project_id, exc_info=True)
            delivered = False
        if not delivered:
            continue
        try:
            db.claim_notification(f"pwatch:day:{day}")
            db.claim_notification(f"pwalert:{stall.project_id}:{stall.severity}:{day}")
            db.claim_notification(f"pwslot:{stall.project_id}:{stall.situation}:{slot}")
        except Exception:  # noqa: BLE001 - le message est parti : on le compte quand même
            log.warning("surveillance des projets : clés non posées pour %s", stall.project_id, exc_info=True)
        return 1
    return 0
