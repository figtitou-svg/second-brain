"""Projets : logique PURE (aucune E/S) — catégorie, rattachement automatique, avancement, cartes HTML Telegram.

Une tâche appartient à un projet par sa catégorie (le préfixe `[IA]` de son titre) ou par une proposition validée.
Les fonctions qui touchent à la base sont dans coach/project_service.py.
"""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

from coach import fmt
from coach.focus import subject_of
from coach.models import Milestone, Project, Task

OPEN = ("pending", "scheduled")
BAR_WIDTH = 6
MAX_TASKS_SHOWN = 5   # tâches ouvertes listées par étape dans la fiche (le reste : « … et N autres »)
MAX_CHARS = 4000      # Telegram refuse un message de plus de 4096 caractères
TRUNCATED = "… (liste tronquée)"


def fold(text: str) -> str:
    """Minuscules sans accents, pour comparer."""
    return "".join(c for c in unicodedata.normalize("NFD", text.strip().lower()) if unicodedata.category(c) != "Mn")


def resolve_category(text: str, subjects: dict[str, str], extra_codes: Iterable[str] = ()) -> str | None:
    """Code court (préfixe de titre) d'une catégorie saisie : nom complet Notion, code, ou texte tel quel."""
    t = text.strip().strip("[]").strip()
    if not t:
        return None
    f = fold(t)
    for name, code in subjects.items():
        if fold(name) == f:
            return code
    for code in set(subjects.values()) | set(extra_codes):
        if fold(code) == f:
            return code
    return t


def category_owner(projs: Iterable[Project], category: str, exclude_id: str | None = None) -> Project | None:
    """Projet non terminé qui possède déjà cette catégorie."""
    f = fold(category)
    for p in projs:
        if p.status != "done" and p.id != exclude_id and p.category and fold(p.category) == f:
            return p
    return None


def auto_project(task: Task, projs: Iterable[Project]) -> Project | None:
    """Projet (actif ou en pause) dont la catégorie est le préfixe du titre de la tâche."""
    cat = subject_of(task.title)
    if not cat:
        return None
    f = fold(cat)
    for p in projs:
        if p.status in ("active", "paused") and p.category and fold(p.category) == f:
            return p
    return None


@dataclass
class Progress:
    done_milestones: int
    total_milestones: int
    current: Milestone | None      # première étape ouverte
    current_done: int              # tâches faites de l'étape en cours
    current_total: int
    tasks_done: int                # tâches du projet (sans les abandonnées)
    tasks_total: int
    next_task: Task | None
    last_activity: datetime | None
    days_left: int | None


def progress(project: Project, milestones: list[Milestone], tasks: list[Task], now: datetime) -> Progress:
    live = [t for t in tasks if t.status != "dropped"]
    ms = sorted(milestones, key=lambda m: m.position)
    current = next((m for m in ms if m.status == "open"), None)
    in_current = [t for t in live if current is not None and t.milestone_id == current.id]
    open_tasks = [t for t in live if t.status in OPEN]
    pool = [t for t in open_tasks if current is not None and t.milestone_id == current.id] or open_tasks
    next_task = min(pool, key=lambda t: (t.due is None, t.due.timestamp() if t.due else 0, t.title), default=None)
    return Progress(
        done_milestones=sum(1 for m in ms if m.status == "done"), total_milestones=len(ms), current=current,
        current_done=sum(1 for t in in_current if t.status == "done"), current_total=len(in_current),
        tasks_done=sum(1 for t in live if t.status == "done"), tasks_total=len(live), next_task=next_task,
        last_activity=project.last_activity,
        days_left=(project.deadline - now.date()).days if project.deadline else None)


def milestone_ready(milestone: Milestone, tasks: list[Task]) -> bool:
    """Toutes les tâches de l'étape sont faites (et il y en a au moins une) : le bot peut proposer de la franchir."""
    mine = [t for t in tasks if t.milestone_id == milestone.id and t.status != "dropped"]
    return bool(mine) and all(t.status == "done" for t in mine)


# --- rendu -----------------------------------------------------------------------------

def bar(done: int, total: int, width: int = BAR_WIDTH) -> str:
    if total <= 0:
        return "░" * width
    if done >= total:
        filled = width
    else:  # commencé : au moins une case ; pas fini : jamais pleine
        filled = max(1, min(width - 1, int(width * done / total))) if done > 0 else 0
    return "▓" * filled + "░" * (width - filled)


def cap_lines(lines: list[str], limit: int = MAX_CHARS) -> str:
    """Les lignes jointes ; au-delà de `limit` caractères, coupe des lignes ENTIÈRES et ajoute « liste tronquée »."""
    text = "\n".join(lines)
    if len(text) <= limit:
        return text
    kept: list[str] = []
    size = len(TRUNCATED) + 1
    for line in lines:
        size += len(line) + 1
        if size > limit:
            break
        kept.append(line)
    return "\n".join(kept + [TRUNCATED])


def _ago(last: datetime | None, now: datetime) -> str:
    if last is None:
        return "aucune activité"
    days = (now.date() - last.astimezone(now.tzinfo).date()).days
    if days <= 0:
        return "dernière activité aujourd'hui"
    return "dernière activité hier" if days == 1 else f"dernière activité il y a {days} j"


def _deadline(project: Project, prog: Progress) -> str:
    if project.deadline is None:
        return ""
    when = fmt.day_short(project.deadline)
    d = prog.days_left or 0
    if d < 0:
        return f" · rendu {when} (en retard de {-d} j)"
    return f" · rendu {when} (aujourd'hui)" if d == 0 else f" · rendu {when} (dans {d} j)"


def overview_card(project: Project, prog: Progress, now: datetime) -> str:
    lines = [f"📁 <b>{fmt.esc(project.name)}</b>{_deadline(project, prog)}"]
    if prog.total_milestones:
        if prog.current is None:
            lines.append(f"{bar(prog.done_milestones, prog.total_milestones)} ✅ Toutes les étapes sont franchies")
        else:
            lines.append(f"{bar(prog.done_milestones, prog.total_milestones)} Étape "
                         f"{prog.done_milestones + 1}/{prog.total_milestones} : <i>{fmt.esc(prog.current.title)}</i> · "
                         f"{prog.current_done}/{prog.current_total} tâches")
    else:
        lines.append(f"{bar(prog.tasks_done, prog.tasks_total)} {prog.tasks_done}/{prog.tasks_total} tâches")
    if prog.next_task is not None:
        due = f" (avant {fmt.day_short(prog.next_task.due)})" if prog.next_task.due else ""
        lines.append(f"Prochaine tâche : {fmt.esc(prog.next_task.title)}{due}")
    else:
        lines.append("Aucune tâche ouverte")
    lines.append(f"<i>{_ago(prog.last_activity, now)}</i>")
    return "\n".join(lines)


def detail_card(project: Project, milestones: list[Milestone], tasks: list[Task], prog: Progress,
                now: datetime) -> str:
    icon = {"active": "📁", "paused": "⏸", "done": "🏁"}[project.status]
    lines = [f"{icon} <b>{fmt.esc(project.name)}</b>{_deadline(project, prog)}"]
    if project.category:
        lines.append(f"Catégorie : <code>{fmt.esc(project.category)}</code>")
    for key, label in (("goal", "🎯 Objectif"), ("limits", "🚧 Limites"), ("deliverables", "📦 Livrables"),
                       ("team", "👥 Équipe"), ("weekly_hours", "⏱ Temps par semaine")):
        if project.profile.get(key):
            lines.append(f"{label} : {fmt.esc(project.profile[key])}")
    open_tasks = [t for t in tasks if t.status in OPEN]

    def listed(group: list[Task]) -> list[str]:
        out = [f"      • {fmt.esc(t.title)}" for t in group[:MAX_TASKS_SHOWN]]
        if len(group) > MAX_TASKS_SHOWN:
            out.append(f"      … et {len(group) - MAX_TASKS_SHOWN} autres")
        return out

    if milestones:
        lines.append("")
        for m in sorted(milestones, key=lambda x: x.position):
            lines.append(f"{'✅' if m.status == 'done' else '⏳'} {fmt.esc(m.title)}")
            lines += listed([t for t in open_tasks if t.milestone_id == m.id])
    loose = [t for t in open_tasks if not t.milestone_id or t.milestone_id not in {m.id for m in milestones}]
    if loose:
        lines.append("")
        lines.append("Sans étape :")
        lines += listed(loose)
    lines.append("")
    lines.append(f"{bar(prog.done_milestones, prog.total_milestones) if prog.total_milestones else bar(prog.tasks_done, prog.tasks_total)}"
                 f" · {prog.tasks_done}/{prog.tasks_total} tâches · <i>{_ago(prog.last_activity, now)}</i>")
    return cap_lines(lines)
