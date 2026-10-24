"""Assistant de projet (B1) : suggestions de prochaines tâches, tâches acceptées, rangement dans les étapes.

L'IA propose, l'utilisateur dispose : rien n'est créé ni rangé sans un clic. Un seul appel IA par proposition ;
tout le reste (filtres, doublons, bornes) est du code déterministe et testé.
"""
from __future__ import annotations

import json
import logging
import math
import threading
from datetime import datetime, time, timedelta
from typing import Any, Callable

from pydantic import BaseModel, Field, ValidationError, field_validator

from coach import dedupe, fmt, project_service, projects
from coach.db import Database
from coach.models import Milestone, Project, Suggestion, Task, short_id

log = logging.getLogger("coach.project_assistant")
LLMFn = Callable[..., str]

MAX_SUGGESTIONS = 5
MIN_MINUTES, MAX_MINUTES, DEFAULT_MINUTES = 5, 480, 30
TITLE_MAX = 80
HISTORY = 10
IDLE_AFTER = timedelta(hours=1)    # un projet vide depuis moins d'une heure : l'utilisateur ajoute peut-être déjà ses tâches
STALE = timedelta(days=3)          # un lot non traité depuis ce délai est écarté
MAX_ATTEMPTS = 3                   # appels IA par projet et par jour (pannes comprises)
_LOCK = threading.Lock()  # une seule génération à la fois : deux déclencheurs simultanés ne font pas deux lots
_CLICK_LOCK = threading.RLock()  # un seul clic `ps:` à la fois : un double appui sur 📌 ne planifie pas deux fois

SYSTEM = (
    "Tu aides un étudiant à avancer sur un projet. À partir de la fiche du projet, de ses étapes et de l'historique, "
    "propose 3 à 5 prochaines tâches CONCRÈTES et réalisables (une action claire de 15 minutes à 3 heures), dans "
    "l'ordre, sans répéter ce qui est déjà fait, ouvert, proposé ou refusé, et en respectant les limites du projet. "
    'Réponds UNIQUEMENT avec un JSON {"tasks":[{"title":str,"estimated_minutes":int,"milestone":str|null}]}. '
    "milestone = le titre EXACT d'une étape ouverte, ou null. Aucun texte hors JSON.")


def _clean(text: Any, limit: int) -> str:
    return " ".join(str(text).split())[:limit]


def _json_object(raw: str) -> Any:
    a, b = raw.find("{"), raw.rfind("}")
    if a == -1 or b <= a:
        raise ValueError("pas de JSON dans la réponse")
    return json.loads(raw[a: b + 1])


class _Item(BaseModel):
    title: str | None = None
    estimated_minutes: int | None = None
    milestone: str | None = None

    @field_validator("title", "milestone", mode="before")
    @classmethod
    def _text(cls, v: Any) -> str | None:
        return (v.strip() or None) if isinstance(v, str) else None

    @field_validator("estimated_minutes", mode="before")
    @classmethod
    def _minutes(cls, v: Any) -> int | None:
        if isinstance(v, bool):
            return None
        try:
            if isinstance(v, str):
                v = float(v.strip())
            if isinstance(v, (int, float)) and math.isfinite(v):
                return int(v)
        except (ValueError, OverflowError):  # « abc », « 1e999 » : durée par défaut
            pass
        return None


class _Response(BaseModel):
    tasks: list[_Item] = Field(default_factory=list)


def _ask(llm: LLMFn | None, sensitivity: str, system: str, prompt: str, model: type[BaseModel]) -> Any | None:
    """Un essai + un nouvel essai si le JSON est invalide ; None si l'IA est indisponible ou deux fois invalide."""
    if llm is None:
        return None
    err: Exception | None = None
    for attempt in range(2):
        p = prompt if attempt == 0 else (
            f"{prompt}\n\nTa réponse précédente était invalide ({err}). Réponds uniquement avec le JSON demandé.")
        try:
            raw = llm(p, sensitivity=sensitivity, system=system, json_mode=True)
        except Exception:  # noqa: BLE001 - quota, réseau, refus du routeur : l'assistant se tait
            log.warning("assistant de projet : IA indisponible", exc_info=True)
            return None
        try:
            return model.model_validate(_json_object(str(raw)))
        except (ValueError, ValidationError) as e:
            err = e
    log.warning("assistant de projet : réponse invalide deux fois (%s)", err)
    return None


def build_prompt(db: Database, project: Project, target: Milestone | None, now: datetime) -> str:
    """Contexte envoyé à l'IA : fiche, étapes, étape visée, historique de tâches et de suggestions."""
    ms = sorted(db.list_milestones(project.id), key=lambda m: m.position)
    tasks = db.list_project_tasks(project.id)
    lines = [f"Date actuelle : {now.date().isoformat()}", f"Projet : {_clean(project.name, 80)}"]
    if project.category:
        lines.append(f"Catégorie : {project.category}")
    for key, label in (("goal", "Objectif"), ("limits", "Limites"), ("deliverables", "Livrables"),
                       ("team", "Équipe"), ("weekly_hours", "Temps par semaine")):
        if project.profile.get(key):
            lines.append(f"{label} : {_clean(project.profile[key], 300)}")
    if project.deadline:
        lines.append(f"Rendu : {project.deadline.isoformat()}")
    if ms:
        lines.append("Étapes :")
        lines += [f"- {'faite' if m.status == 'done' else 'ouverte'} : {_clean(m.title, 80)}"
                  + (f" (avant {m.due.isoformat()})" if m.due else "") for m in ms]
    lines.append("Étape visée : " + (_clean(target.title, 80) if target else "aucune (projet sans étape, ou toutes franchies)"))
    done = [t for t in tasks if t.status == "done"][-HISTORY:]
    still_open = [t for t in tasks if t.status in projects.OPEN][-HISTORY:]
    if done:
        lines.append("Dernières tâches faites : " + " | ".join(_clean(t.title, 80) for t in done))
    if still_open:
        lines.append("Tâches déjà ouvertes : " + " | ".join(_clean(t.title, 80) for t in still_open))
    past = db.list_project_suggestions(project.id, HISTORY)
    if past:
        fate = {"added": "acceptée", "planned": "acceptée", "dismissed": "écartée", "proposed": "en attente"}
        lines.append("Suggestions déjà faites (à ne pas répéter) : "
                     + " | ".join(f"{_clean(s.title, 80)} ({fate[s.state]})" for s in past))
    return "\n".join(lines)


def _filter(db: Database, project: Project, items: list[_Item], target: Milestone | None) -> list[tuple[str, int, str | None]]:
    """(titre, minutes, id d'étape) retenus : sans doublon, bornés, au plus MAX_SUGGESTIONS."""
    by_title: dict[str, Milestone] = {}
    for m in db.list_milestones(project.id):
        if m.status == "open":
            by_title.setdefault(projects.fold(m.title), m)
    known = [t.title for t in db.list_tasks()] + [s.title for s in db.list_project_suggestions(project.id)]
    out: list[tuple[str, int, str | None]] = []
    for it in items:
        title = _clean(it.title or "", TITLE_MAX)
        if not title or any(dedupe.same_title(title, k) for k in known + [o[0] for o in out]):
            continue
        wanted = DEFAULT_MINUTES if it.estimated_minutes is None else it.estimated_minutes
        minutes = max(MIN_MINUTES, min(MAX_MINUTES, wanted))
        step = by_title.get(projects.fold(it.milestone)) if it.milestone else None
        step = step or target
        out.append((title, minutes, step.id if step else None))
        if len(out) >= MAX_SUGGESTIONS:
            break
    return out


def suggest(db: Database, llm: LLMFn | None, sensitivity: str, project: Project, target: Milestone | None,
            now: datetime) -> list[Suggestion] | None:
    """Génère et enregistre un lot de suggestions. `None` : IA en panne ; `[]` : rien à proposer (réponse vide ou
    entièrement filtrée) ou un lot est déjà en attente (alors aucun appel IA)."""
    with _LOCK:
        if db.pending_batch(project.id):
            return []
        resp = _ask(llm, sensitivity, SYSTEM, build_prompt(db, project, target, now), _Response)
        if resp is None:
            return None
        kept = _filter(db, project, resp.tasks, target)
        if not kept:
            return []
        batch = short_id()
        items = [Suggestion(batch=batch, project_id=project.id, milestone_id=mid, title=title,
                            estimated_minutes=minutes, created_at=now) for title, minutes, mid in kept]
        db.save_suggestions(items)
        return items


# --- rendu et clics -----------------------------------------------------------------------

Buttons = list[list[tuple[str, str]]]
ICON = {"proposed": "", "added": "✅ ", "planned": "📌 ", "dismissed": "✖ "}
Planner = Callable[[str], list]  # id de tâche -> blocs placés par le planificateur ([] : aucun créneau)


def render_batch(db: Database, batch: str, note: str | None = None, buttons: bool = True) -> tuple[str, Buttons]:
    """Message d'un lot : une ligne par suggestion, des boutons seulement pour celles encore `proposed`."""
    items = db.list_batch(batch)
    if not items:
        return "❓ Lot introuvable.", []
    pid = items[0].project_id
    p = db.get_project(pid)
    titles = {m.id: m.title for m in db.list_milestones(pid)}
    head_step = titles.get(items[0].milestone_id) if items[0].milestone_id else None
    lines = [f"💡 Pour la suite de <b>{fmt.esc(p.name if p else '?')}</b>"
             + (f" (étape <i>{fmt.esc(head_step)}</i>)" if head_step else "") + ", je te propose :"]
    rows: Buttons = []
    for i, s in enumerate(items, 1):
        extra = ""
        if s.milestone_id and s.milestone_id != items[0].milestone_id and s.milestone_id in titles:
            extra = f" · <i>{fmt.esc(titles[s.milestone_id])}</i>"
        lines.append(f"{i}. {ICON[s.state]}{fmt.esc(s.title)} · {fmt.dur(s.estimated_minutes)}{extra}")
        if s.state == "proposed":
            rows.append([(f"➕ {i}", f"ps:add:{s.id}"), (f"📌 {i}", f"ps:plan:{s.id}"), (f"✖ {i}", f"ps:no:{s.id}")])
    if rows:
        rows.append([("Tout ajouter", f"ps:all:{batch}"), ("Rien", f"ps:none:{batch}")])
    else:
        count = lambda state: sum(1 for s in items if s.state == state)  # noqa: E731
        lines.append(f"<i>{count('added')} ajoutée(s), {count('planned')} planifiée(s), {count('dismissed')} écartée(s)</i>")
        rows.append([("📂 Voir le projet", f"pjv:{pid}")])
    text = "\n".join(lines)
    return (f"{note}\n\n{text}" if note else text), (rows if buttons else [])


def create_task(db: Database, project: Project, s: Suggestion, now: datetime) -> Task:
    """Vraie tâche issue d'une suggestion acceptée (projet et étape posés, titre préfixé par la catégorie)."""
    step = db.get_milestone(s.milestone_id) if s.milestone_id else None
    if step is not None and step.project_id != project.id:
        step = None
    title = s.title
    if project.category and not title.lstrip().startswith("["):
        title = f"[{project.category}] {title}"
    day = (step.due if step is not None and step.due else None) or project.deadline
    task = Task(title=title, estimated_minutes=s.estimated_minutes, priority="medium",
                due=datetime.combine(day, time(23, 59), tzinfo=now.tzinfo) if day else None,
                source_id=f"suggestion:{s.id}", project_id=project.id, milestone_id=step.id if step else None)
    db.upsert_task(task)
    db.add_project_example(title, project.id, task.milestone_id)
    db.touch_project(project.id, now)
    return task


def _accept(db: Database, s: Suggestion, project: Project, now: datetime, plan: Planner | None) -> str | None:
    """Accepte une suggestion `proposed` ; renvoie une note à afficher, ou None."""
    existing = db.list_tasks()
    # Tâche déjà créée pour cette suggestion (clic précédent interrompu) : on la reprend au lieu de la dédoublonner.
    task = next((t for t in existing if t.source_id == f"suggestion:{s.id}"), None)
    if task is None:
        if dedupe.find_task(Task(title=s.title), existing) is not None:
            db.set_suggestion_state(s.id, "added")
            return f"👌 « {fmt.esc(s.title)} » est déjà dans tes tâches."
        task = create_task(db, project, s, now)
    if plan is None:
        db.set_suggestion_state(s.id, "added")
        return None
    try:
        blocks = plan(task.id)
    except Exception:
        log.warning("planification de la suggestion %s impossible", s.id, exc_info=True)
        blocks = []
    if not blocks:
        db.set_suggestion_state(s.id, "added")
        return f"⚠️ « {fmt.esc(s.title)} » est ajoutée, mais aucun créneau n'a été trouvé."
    db.set_suggestion_state(s.id, "planned")
    return f"📌 Planifiée : {fmt.when(blocks[0].start)}–{fmt.hm(blocks[0].end)}"


def click(db: Database, action: str, arg: str, now: datetime, plan: Planner | None = None) -> tuple[str, Buttons]:
    """Boutons `ps:` : `add`, `plan`, `no` (une suggestion) ; `all`, `none` (un lot).
    Un seul clic à la fois : le bot traite les mises à jour en parallèle, un double appui ne planifie pas deux fois."""
    with _CLICK_LOCK:
        return _click(db, action, arg, now, plan)


def _click(db: Database, action: str, arg: str, now: datetime, plan: Planner | None) -> tuple[str, Buttons]:
    if action in ("add", "plan", "no"):
        s = db.get_suggestion(arg)
        if s is None:
            return "❓ Suggestion introuvable.", []
        if s.state != "proposed":
            return render_batch(db, s.batch, "👌 Déjà traité, rien n'a changé.")
        project = db.get_project(s.project_id)
        if project is None or project.status != "active":
            return render_batch(db, s.batch, "👌 Ce projet est en pause ou terminé, rien n'a changé.", buttons=False)
        if action == "no":
            db.set_suggestion_state(s.id, "dismissed")
            return render_batch(db, s.batch)
        return render_batch(db, s.batch, _accept(db, s, project, now, plan if action == "plan" else None))
    if action in ("all", "none"):
        items = db.list_batch(arg)
        if not items:
            return "❓ Lot introuvable.", []
        todo = [s for s in items if s.state == "proposed"]
        if not todo:
            return render_batch(db, arg, "👌 Déjà traité, rien n'a changé.")
        project = db.get_project(items[0].project_id)
        if project is None or project.status != "active":
            return render_batch(db, arg, "👌 Ce projet est en pause ou terminé, rien n'a changé.", buttons=False)
        for s in todo:
            if action == "none":
                db.set_suggestion_state(s.id, "dismissed")
            else:
                _accept(db, s, project, now, None)
        return render_batch(db, arg)
    return "❓ Action inconnue.", []


# --- déclencheurs -------------------------------------------------------------------------

def after_milestone_crossed(db: Database, llm: LLMFn | None, sensitivity: str, project_id: str,
                            now: datetime) -> tuple[str, Buttons] | None:
    """Suggestions pour l'étape suivante, à ajouter à la réponse du « Oui » à « Étape franchie ? »."""
    p = db.get_project(project_id)
    if p is None or p.status != "active":
        return None
    open_steps = [m for m in sorted(db.list_milestones(p.id), key=lambda m: m.position) if m.status == "open"]
    if not open_steps:
        return None
    items = suggest(db, llm, sensitivity, p, open_steps[0], now)
    return render_batch(db, items[0].batch) if items else None


def go(db: Database, llm: LLMFn | None, sensitivity: str, project_id: str, now: datetime) -> tuple[str, Buttons]:
    """Bouton « 💡 Relancer » d'une alerte de projet (`ps:go:<projet>`) : propose des tâches pour l'étape en cours.
    N'est PAS protégé par le verrou des clics : l'appel IA peut être lent, les autres boutons restent libres."""
    p = db.get_project(project_id)
    if p is None or p.status != "active":
        return "👌 Ce projet est en pause ou terminé, rien n'a changé.", []
    pending = db.pending_batch(p.id)
    if pending:
        return render_batch(db, pending)   # des suggestions attendent déjà un clic : on les remontre
    open_steps = [m for m in sorted(db.list_milestones(p.id), key=lambda m: m.position) if m.status == "open"]
    items = suggest(db, llm, sensitivity, p, open_steps[0] if open_steps else None, now)
    if items:
        return render_batch(db, items[0].batch)
    pending = db.pending_batch(p.id)
    if items == [] and pending:
        return render_batch(db, pending)   # un autre déclencheur a créé un lot entre-temps
    if items is None:
        text = "🤔 Je n'ai pas réussi à proposer des tâches pour le moment. Réessaie plus tard."
    else:
        text = "🤔 Je n'ai pas d'idée de tâche pour le moment."
    return text, [[("📂 Voir le projet", f"pjv:{p.id}")]]


def _idle_one(db: Database, llm: LLMFn | None, sensitivity: str, p: Project, notify: Callable[..., Any],
              now: datetime, day: str) -> int:
    """Traite un projet actif pour `idle_check` ; renvoie 1 si une proposition a été envoyée, sinon 0."""
    if any(t.status in projects.OPEN for t in db.list_project_tasks(p.id)):
        return 0
    steps = sorted(db.list_milestones(p.id), key=lambda m: m.position)
    open_steps = [m for m in steps if m.status == "open"]
    if steps and not open_steps:
        return 0
    last = p.last_activity
    if last is not None:
        if last.tzinfo is None and now.tzinfo is not None:
            last = last.replace(tzinfo=now.tzinfo)   # date naïve en base : on la lit à l'heure locale du planning
        if now - last < IDLE_AFTER:
            return 0
    if db.has_notification(f"psuggest:{p.id}:{day}") or db.pending_batch(p.id):
        return 0
    if not any(db.claim_notification(f"psfail:{p.id}:{day}:{n}") for n in range(1, MAX_ATTEMPTS + 1)):
        return 0   # déjà MAX_ATTEMPTS appels IA aujourd'hui pour ce projet
    items = suggest(db, llm, sensitivity, p, open_steps[0] if open_steps else None, now)
    if items is None:
        return 0   # IA en panne : on réessaiera au prochain passage (dans la limite des essais du jour)
    if not items:
        db.claim_notification(f"psuggest:{p.id}:{day}")   # réponse valide mais vide après filtrage : jour consommé
        return 0
    try:
        text, buttons = render_batch(db, items[0].batch)
        delivered = notify(text, buttons) is not False   # None (notify sans retour) : réputé envoyé
    except Exception:  # noqa: BLE001
        log.warning("assistant de projet : envoi impossible", exc_info=True)
        delivered = False
    if not delivered:
        db.delete_batch(items[0].batch)   # jamais vu par l'utilisateur : on l'oublie, un prochain essai pourra reproposer
        return 0
    db.claim_notification(f"psuggest:{p.id}:{day}")   # réponse valide et envoyée : jour consommé
    return 1


def idle_check(db: Database, llm: LLMFn | None, sensitivity: str, notify: Callable[..., Any], now: datetime) -> int:
    """Propose la suite des projets actifs sans tâche ouverte (au plus une fois par jour et par projet).
    Appelée toutes les 15 minutes, hors du verrou du scheduler. Renvoie le nombre de messages envoyés.
    Ne lève jamais : un projet en échec est journalisé et n'empêche pas les suivants."""
    try:
        db.dismiss_stale_suggestions(now - STALE)
    except Exception:  # noqa: BLE001 - une ligne illisible ne doit pas empêcher les propositions
        log.warning("assistant de projet : nettoyage des lots périmés impossible", exc_info=True)
    try:
        todo = db.list_projects("active")
    except Exception:  # noqa: BLE001
        log.warning("assistant de projet : passage inactif impossible", exc_info=True)
        return 0
    sent, day = 0, now.date().isoformat()
    for p in todo:
        try:
            sent += _idle_one(db, llm, sensitivity, p, notify, now, day)
        except Exception:  # noqa: BLE001 - un projet en échec ne doit pas arrêter les autres ni la tâche planifiée
            log.warning("assistant de projet : proposition impossible pour %s", p.id, exc_info=True)
    return sent


# --- création directe des tâches des étapes ---------------------------------------------------

FILL_PER_STEP, FILL_MAX = 3, 9
_FILL_LOCK = threading.Lock()   # un seul « Créer les tâches » à la fois : un double appui ne crée pas deux fois
FILL_SYSTEM = (
    "Tu découpes les étapes d'un projet étudiant en tâches CONCRÈTES et réalisables (une action claire de 15 minutes "
    "à 3 heures). On te donne la fiche du projet et la liste des étapes À COUVRIR, qui n'ont aucune tâche ouverte. "
    f"Pour chaque étape listée, propose 1 à {FILL_PER_STEP} tâches, dans l'ordre, sans répéter l'existant ni ce qui est "
    "refusé, en respectant les limites du projet. N'invente rien : si une étape ne demande aucune action concrète, "
    "n'en propose pas pour elle. "
    'Réponds UNIQUEMENT avec un JSON {"tasks":[{"title":str,"estimated_minutes":int,"milestone":str}]}. '
    "milestone = le titre EXACT de l'étape listée. Aucun texte hors JSON.")


def _fill_filter(db: Database, project: Project, items: list[_Item], gaps: list[Milestone]) -> list[tuple[str, int, Milestone]]:
    """(titre, minutes, étape) retenus : étape à couvrir exacte (jamais devinée), sans doublon, bornés."""
    by_title = {}
    for m in gaps:
        by_title.setdefault(projects.fold(m.title), m)
    known = [t.title for t in db.list_tasks()] + [s.title for s in db.list_project_suggestions(project.id)]
    per_step: dict[str, int] = {}
    out: list[tuple[str, int, Milestone]] = []
    for it in items:
        step = by_title.get(projects.fold(it.milestone)) if it.milestone else None
        title = _clean(it.title or "", TITLE_MAX)
        if step is None or not title or per_step.get(step.id, 0) >= FILL_PER_STEP:
            continue
        if any(dedupe.same_title(title, k) for k in known + [o[0] for o in out]):
            continue
        wanted = DEFAULT_MINUTES if it.estimated_minutes is None else it.estimated_minutes
        out.append((title, max(MIN_MINUTES, min(MAX_MINUTES, wanted)), step))
        per_step[step.id] = per_step.get(step.id, 0) + 1
        if len(out) >= FILL_MAX:
            break
    return out


def fill_steps(db: Database, llm: LLMFn | None, sensitivity: str, project_id: str,
               now: datetime) -> tuple[str, Buttons]:
    """Bouton « 🧩 Créer les tâches des étapes » : crée directement des tâches pour les étapes ouvertes qui n'en ont
    aucune. Rien n'est créé pour une étape déjà servie, ni quand l'IA n'a rien d'utile à proposer."""
    with _FILL_LOCK:
        p = db.get_project(project_id)
        if p is None or p.status != "active":
            return "👌 Ce projet est en pause ou terminé, rien n'a changé.", []
        gaps = project_service.step_gaps(db, p.id)
        if not gaps:
            return "👌 Chaque étape ouverte a déjà une tâche, rien à créer.", _see_project(p.id)
        todo = "\n".join(f"- {_clean(m.title, 80)}" for m in gaps)
        prompt = f"{build_prompt(db, p, gaps[0], now)}\nÉtapes à couvrir (aucune tâche ouverte) :\n{todo}"
        resp = _ask(llm, sensitivity, FILL_SYSTEM, prompt, _Response)
        if resp is None:
            return "🤔 Je n'ai pas réussi à créer les tâches pour le moment. Réessaie plus tard.", _see_project(p.id)
        kept = _fill_filter(db, p, resp.tasks, gaps)
        if not kept:
            return "🤔 Je n'ai pas trouvé de tâche utile à créer pour ces étapes.", _see_project(p.id)
        batch = short_id()
        made = [Suggestion(batch=batch, project_id=p.id, milestone_id=m.id, title=title, estimated_minutes=minutes,
                           state="added", created_at=now) for title, minutes, m in kept]
        db.save_suggestions(made)   # garde la trace : l'assistant ne les reproposera pas
        for s in made:
            create_task(db, p, s, now)
        lines = [f"🧩 <b>{len(made)} tâche(s) créée(s)</b> pour <b>{fmt.esc(p.name)}</b> :"]
        for m in gaps:
            mine = [s for s in made if s.milestone_id == m.id]
            if mine:
                lines.append(f"<i>{fmt.esc(m.title)}</i>")
                lines += [f"• {fmt.esc(s.title)} · {fmt.dur(s.estimated_minutes)}" for s in mine]
        return "\n".join(lines), _see_project(p.id)


# --- rangement en lot ----------------------------------------------------------------------

RANK_SENT, RANK_PROPOSED = 30, 15
RANK_SYSTEM = (
    "Tu ranges les tâches d'un projet dans ses étapes. On te donne les étapes ouvertes et des tâches numérotées. "
    'Réponds UNIQUEMENT avec un JSON {"assign":[{"n":int,"step":str|null}]} : step = le titre EXACT de l\'étape qui '
    "correspond le mieux à la tâche numéro n, ou null si aucune ne convient. Aucun texte hors JSON.")


class _Assign(BaseModel):
    n: int | None = None
    step: str | None = None

    @field_validator("n", mode="before")
    @classmethod
    def _n(cls, v: Any) -> int | None:
        return v if isinstance(v, int) and not isinstance(v, bool) else None

    @field_validator("step", mode="before")
    @classmethod
    def _step(cls, v: Any) -> str | None:
        return (v.strip() or None) if isinstance(v, str) else None


class _RankResponse(BaseModel):
    assign: list[_Assign] = Field(default_factory=list)


def _see_project(pid: str) -> Buttons:
    return [[("📂 Voir le projet", f"pjv:{pid}")]]


def render_rank(db: Database, project_id: str, note: str | None = None) -> tuple[str, Buttons]:
    """Message du rangement en lot, recalculé depuis la base (lignes traitées marquées, boutons pour celles en attente)."""
    p = db.get_project(project_id)
    steps = {m.id: m.title for m in db.list_milestones(project_id)}
    lines = [f"🗂 Je propose de ranger les tâches de <b>{fmt.esc(p.name if p else '?')}</b> :"]
    rows: Buttons = []
    counts = {"lot_ok": 0, "lot_no": 0}
    shown = 0
    for r in db.list_lot_proposals(project_id):
        task = db.get_task(r["task_id"])
        if task is None:
            continue
        shown += 1
        icon = {"lot": "", "lot_ok": "✅ ", "lot_no": "✖ "}[r["state"]]
        counts[r["state"]] = counts.get(r["state"], 0) + 1
        lines.append(f"{shown}. {icon}{fmt.esc(_clean(task.title, TITLE_MAX))} → <i>{fmt.esc(steps.get(r['milestone_id'], '?'))}</i>")
        if r["state"] == "lot":
            rows.append([(f"✅ {shown}", f"pa:ok:{task.id}"), (f"✖ {shown}", f"pa:no:{task.id}")])
    if rows:
        rows.append([("Tout valider", f"pa:all:{project_id}"), ("Rien", f"pa:none:{project_id}")])
    else:
        lines.append(f"<i>{counts['lot_ok']} rangée(s), {counts['lot_no']} écartée(s)</i>")
        rows = _see_project(project_id)
    text = "\n".join(lines)
    return (f"{note}\n\n{text}" if note else text), rows


def rank_batch(db: Database, llm: LLMFn | None, sensitivity: str, project_id: str,
               now: datetime) -> tuple[str, Buttons]:
    """Bouton « Ranger les tâches » : un appel IA propose une étape par tâche sans étape ; rien n'est rangé sans clic."""
    p = db.get_project(project_id)
    if p is None or p.status != "active":
        return "👌 Ce projet n'est plus actif, rien n'a changé.", []
    open_steps = [m for m in sorted(db.list_milestones(p.id), key=lambda m: m.position) if m.status == "open"]
    cands = project_service.rank_candidates(db, p.id)[:RANK_SENT]
    if not open_steps or not cands:
        return "👌 Rien à ranger pour ce projet.", _see_project(p.id)
    lines = [f"Projet : {_clean(p.name, 80)}"]
    for key, label in (("goal", "Objectif"), ("limits", "Limites")):
        if p.profile.get(key):
            lines.append(f"{label} : {_clean(p.profile[key], 200)}")
    lines.append("Étapes ouvertes :")
    lines += [f"- ouverte : {_clean(m.title, 80)}" for m in open_steps]
    lines.append("Tâches :")
    lines += [f"{i}. {_clean(t.title, 100)}" for i, t in enumerate(cands, 1)]
    resp = _ask(llm, sensitivity, RANK_SYSTEM, "\n".join(lines), _RankResponse)
    if resp is None:
        return "🤔 Je n'ai pas réussi à ranger les tâches pour le moment. Réessaie plus tard.", _see_project(p.id)
    by_step: dict[str, Milestone] = {}
    for m in open_steps:
        by_step.setdefault(projects.fold(m.title), m)
    chosen: dict[str, str] = {}
    for a in resp.assign:
        if a.n is None or not 1 <= a.n <= len(cands) or not a.step:
            continue
        step = by_step.get(projects.fold(a.step))
        if step is not None and cands[a.n - 1].id not in chosen and len(chosen) < RANK_PROPOSED:
            chosen[cands[a.n - 1].id] = step.id
    if not chosen:
        return "🤔 Je n'ai pas trouvé d'étape évidente pour ces tâches.", _see_project(p.id)
    db.clear_lot_proposals(p.id)
    for task_id, step_id in chosen.items():
        db.set_proposal(task_id, p.id, step_id, "lot")
    return render_rank(db, p.id)


def _lot_accept(db: Database, task: Any, prop: dict, project: Project, now: datetime) -> str | None:
    """Range une tâche d'un lot ; None si c'est fait, sinon une note (étape disparue : la ligne est écartée)."""
    step = db.get_milestone(prop["milestone_id"]) if prop.get("milestone_id") else None
    if task.project_id != project.id or task.milestone_id or step is None or step.status != "open" \
            or step.project_id != project.id:
        db.set_proposal(task.id, project.id, prop.get("milestone_id"), "lot_no")
        return "⚠️ L'étape n'existe plus ou la tâche a changé : cette ligne est écartée."
    db.set_task_project(task.id, project.id, step.id)
    db.set_proposal(task.id, project.id, step.id, "lot_ok")
    db.add_project_example(task.title, project.id, step.id)
    db.touch_project(project.id, now)
    return None


def rank_click(db: Database, action: str, arg: str, now: datetime) -> tuple[str, Buttons]:
    """Boutons `pa:` du rangement : `ok` / `no` (une tâche), `all` / `none` (un projet)."""
    if action in ("ok", "no"):
        task, prop = db.get_task(arg), db.get_proposal(arg)
        if task is None or not prop or not str(prop.get("state") or "").startswith("lot"):
            return "❓ Plus de proposition pour cette tâche.", []
        pid = prop["project_id"]
        if prop["state"] != "lot":
            return render_rank(db, pid, "👌 Déjà traité, rien n'a changé.")
        project = db.get_project(pid)
        if project is None or project.status != "active":
            return "👌 Ce projet est en pause ou terminé, rien n'a changé.", []
        if action == "no":
            db.set_proposal(task.id, pid, prop.get("milestone_id"), "lot_no")
            return render_rank(db, pid)
        return render_rank(db, pid, _lot_accept(db, task, prop, project, now))
    if action in ("all", "none"):
        project = db.get_project(arg)
        if project is None:
            return "❓ Projet introuvable.", []
        lot = db.list_lot_proposals(project.id)
        todo = [r for r in lot if r["state"] == "lot"]
        if not todo:
            return (render_rank(db, project.id, "👌 Déjà traité, rien n'a changé.") if lot
                    else ("❓ Plus de proposition pour ce projet.", []))
        if project.status != "active":
            return "👌 Ce projet est en pause ou terminé, rien n'a changé.", []
        for r in todo:
            task = db.get_task(r["task_id"])
            if task is None:
                continue
            if action == "none":
                db.set_proposal(task.id, project.id, r["milestone_id"], "lot_no")
            else:
                _lot_accept(db, task, db.get_proposal(task.id) or r, project, now)
        return render_rank(db, project.id)
    return "❓ Action inconnue.", []
