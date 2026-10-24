"""Projets avec la base : rattachement des tâches, propositions, vues et actions.

La logique pure (catégorie, avancement, cartes) est dans coach/projects.py ; l'entretien de création dans
coach/project_interview.py.
"""
from __future__ import annotations

from datetime import datetime

from coach import fmt, projects
from coach.db import Database
from coach.focus import subject_of
from coach.models import Milestone, Task

Buttons = list[list[tuple[str, str]]]
EXAMPLES_PER_PROJECT = 5


# --- rattachement automatique ------------------------------------------------------------

def attach_by_category(db: Database, task: Task) -> Task:
    """La tâche, avec `project_id` posé si son préfixe est la catégorie d'un projet (aucune écriture)."""
    if task.project_id:
        return task
    p = projects.auto_project(task, db.list_projects("active", "paused"))
    return task.model_copy(update={"project_id": p.id}) if p else task


def attach_open_tasks(db: Database, project) -> int:
    """À la création d'un projet : rattache ses tâches ouvertes déjà préfixées de sa catégorie. Renvoie leur nombre."""
    if not project.category:
        return 0
    f = projects.fold(project.category)
    n = 0
    for t in db.list_tasks("pending", "scheduled"):
        cat = subject_of(t.title)
        if t.project_id is None and cat and projects.fold(cat) == f:
            db.set_task_project(t.id, project.id, None)
            n += 1
    return n


# --- proposition de rattachement (IA) ----------------------------------------------------

def extraction_context(db: Database) -> list[dict]:
    """Projets actifs décrits à l'IA d'extraction : nom, catégorie, étapes ouvertes, derniers exemples confirmés."""
    out = []
    for p in db.list_projects("active"):
        all_ms = db.list_milestones(p.id)
        titles = {m.id: m.title for m in all_ms}
        out.append({"name": p.name, "category": p.category,
                    "milestones": [m.title for m in all_ms if m.status == "open"],
                    "examples": [(t, titles.get(mid)) for t, mid in db.recent_examples(p.id, EXAMPLES_PER_PROJECT)]})
    return out


def propose(db: Database, task: Task, project_name: str | None, milestone_title: str | None) -> bool:
    """Enregistre la proposition de l'IA (rien n'est rattaché avant le clic). Les noms inconnus sont ignorés."""
    if not project_name:
        return False
    key = projects.fold(project_name)
    p = next((x for x in db.list_projects("active") if projects.fold(x.name) == key), None)
    if p is None:
        return False
    mid = None
    if milestone_title:
        mk = projects.fold(milestone_title)
        m = next((x for x in db.list_milestones(p.id) if x.status == "open" and projects.fold(x.title) == mk), None)
        mid = m.id if m else None
    db.set_proposal(task.id, p.id, mid, "proposed")
    return True


def propose_step(db: Database, task: Task, project_name: str | None, milestone_title: str | None) -> bool:
    """Proposition d'ÉTAPE pour une tâche déjà dans un projet : l'indice de l'IA doit nommer ce même projet et une
    étape ouverte. Rien n'est écrit sinon."""
    if not project_name or not milestone_title or not task.project_id:
        return False
    p = db.get_project(task.project_id)
    if p is None or p.status != "active" or projects.fold(p.name) != projects.fold(project_name):
        return False
    key = projects.fold(milestone_title)
    step = next((m for m in db.list_milestones(p.id) if m.status == "open" and projects.fold(m.title) == key), None)
    if step is None:
        return False
    db.set_proposal(task.id, p.id, step.id, "proposed")
    return True


def proposal_card(db: Database, task_id: str) -> tuple[str, Buttons] | None:
    """(ligne à ajouter à la carte de la tâche, boutons) ; None s'il n'y a rien à proposer."""
    prop = db.get_proposal(task_id)
    if not prop or prop["state"] != "proposed" or not prop["project_id"]:
        return None
    task = db.get_task(task_id)
    if task is None or task.status in ("done", "dropped"):
        return None
    p = db.get_project(prop["project_id"])
    if p is None or p.status != "active":
        return None
    if task.project_id:  # tâche déjà dans ce projet : on ne propose plus que son étape
        step = db.get_milestone(prop["milestone_id"]) if prop["milestone_id"] else None
        if task.project_id != p.id or task.milestone_id or step is None or step.status != "open":
            return None
        return (f"🧭 Étape <i>{fmt.esc(step.title)}</i> ?",
                [[("Oui", f"pjok:{task_id}"), ("Autre", f"pjwho:{task_id}"), ("Aucune", f"pjno:{task_id}")]])
    line = f"📁 <b>{fmt.esc(p.name)}</b>"
    m = db.get_milestone(prop["milestone_id"]) if prop["milestone_id"] else None
    if m is not None:
        line += f" · étape <i>{fmt.esc(m.title)}</i>"
    return line + " ?", [[("Oui", f"pjok:{task_id}"), ("Autre", f"pjwho:{task_id}"), ("Aucun", f"pjno:{task_id}")]]


def _attach(db: Database, task: Task, project_id: str, milestone_id: str | None, now: datetime) -> tuple[str, Buttons]:
    p = db.get_project(project_id)
    if p is None:
        return "❓ Projet introuvable.", []
    m = db.get_milestone(milestone_id) if milestone_id else None
    if m is None:
        milestone_id = None   # étape supprimée depuis la proposition : rattache au projet sans étape
    db.set_task_project(task.id, project_id, milestone_id)
    db.add_project_example(task.title, project_id, milestone_id)
    db.touch_project(project_id, now)
    where = f" · étape <i>{fmt.esc(m.title)}</i>" if m is not None else ""
    return f"📁 Rattachée à <b>{fmt.esc(p.name)}</b>{where}.", []


def _set_step(db: Database, task: Task, project, step, now: datetime) -> tuple[str, Buttons]:
    db.set_task_project(task.id, project.id, step.id)
    db.set_proposal(task.id, project.id, step.id, "accepted")
    db.add_project_example(task.title, project.id, step.id)
    db.touch_project(project.id, now)
    return f"🧭 Étape <i>{fmt.esc(step.title)}</i> pour cette tâche.", []


def _pick_step(db: Database, action: str, task: Task, prop: dict, arg: str | None, now: datetime) -> tuple[str, Buttons]:
    """Boutons de la proposition d'étape d'une tâche déjà dans un projet."""
    p = db.get_project(task.project_id)
    if p is None or p.status != "active":
        return "👌 Ce projet n'est plus actif, rien n'a changé.", []
    if action == "pjno" or (action == "pjms" and arg in (None, "", "-")):
        db.set_proposal(task.id, prop.get("project_id") or p.id, prop.get("milestone_id"), "none")
        return "👌 D'accord, cette tâche reste sans étape.", []
    if action == "pjok":
        step = db.get_milestone(prop["milestone_id"]) if prop.get("milestone_id") else None
        if prop.get("project_id") != p.id or step is None:
            return "❓ Plus de proposition pour cette tâche.", []
        if step.status != "open":
            return "❓ Étape introuvable.", []
        return _set_step(db, task, p, step, now)
    if action == "pjwho":
        rows = [[(m.title[:28], f"pjms:{task.id}:{m.id}")] for m in db.list_milestones(p.id) if m.status == "open"]
        rows.append([("Aucune", f"pjno:{task.id}")])
        return f"🧭 <b>{fmt.esc(p.name)}</b> : quelle étape ?", rows
    if action == "pjms":
        step = db.get_milestone(arg or "")
        if step is None or step.project_id != p.id or step.status != "open":
            return "❓ Étape introuvable.", []
        return _set_step(db, task, p, step, now)
    return "❓ Action inconnue.", []


def pick(db: Database, action: str, task_id: str, arg: str | None, now: datetime) -> tuple[str, Buttons]:
    """Boutons de la proposition : `pjok` (Oui), `pjwho` (Autre), `pjset` (projet choisi), `pjms` (étape), `pjno`."""
    task = db.get_task(task_id)
    if task is None:
        return "❓ Tâche introuvable.", []
    prop = db.get_proposal(task_id) or {}
    if prop.get("state") == "none" or (task.project_id and task.milestone_id):
        return "👌 Ce rattachement a déjà été traité, rien n'a changé.", []
    if task.project_id:  # déjà dans un projet, sans étape : seule l'étape est en jeu
        return _pick_step(db, action, task, prop, arg, now)
    if action == "pjno":
        db.set_proposal(task_id, prop.get("project_id"), prop.get("milestone_id"), "none")
        return "👌 D'accord, cette tâche reste hors projet.", []
    if action == "pjok":
        if not prop.get("project_id"):
            return "❓ Plus de proposition pour cette tâche.", []
        return _attach(db, task, prop["project_id"], prop.get("milestone_id"), now)
    if action == "pjwho":
        rows = [[(p.name[:28], f"pjset:{task_id}:{p.id}")] for p in db.list_projects("active")]
        rows.append([("Aucun", f"pjno:{task_id}")])
        return "📁 Dans quel projet ?", rows
    if action == "pjset":
        p = db.get_project(arg or "")
        if p is None or p.status != "active":
            return "❓ Projet introuvable.", []
        open_ms = [m for m in db.list_milestones(p.id) if m.status == "open"]
        if not open_ms:
            return _attach(db, task, p.id, None, now)
        db.set_proposal(task_id, p.id, None, "proposed")   # mémorise le projet choisi pour l'étape suivante
        rows = [[(m.title[:28], f"pjms:{task_id}:{m.id}")] for m in open_ms]
        rows.append([("Sans étape", f"pjms:{task_id}:-")])
        return f"📁 <b>{fmt.esc(p.name)}</b> : quelle étape ?", rows
    if action == "pjms":
        if arg and arg != "-":
            m = db.get_milestone(arg)
            if m is None:
                return "❓ Étape introuvable.", []
            return _attach(db, task, m.project_id, m.id, now)
        if not prop.get("project_id"):
            return "❓ Projet introuvable.", []
        return _attach(db, task, prop["project_id"], None, now)
    return "❓ Action inconnue.", []


# --- vues ---------------------------------------------------------------------------------

def overview(db: Database, now: datetime) -> tuple[str, Buttons]:
    """`/projets` : une carte par projet actif, les projets en pause sur une ligne, un bouton par projet."""
    active, paused = db.list_projects("active"), db.list_projects("paused")
    if not active and not paused:
        return "📁 Aucun projet pour l'instant. Crée-en un avec /projet.", []
    parts = ["📁 <b>Tes projets</b>"]
    rows: Buttons = []
    for p in active:
        prog = projects.progress(p, db.list_milestones(p.id), db.list_project_tasks(p.id), now)
        parts.append(projects.overview_card(p, prog, now))
        rows.append([(f"📂 {p.name[:24]}", f"pjv:{p.id}")])
    if paused:
        parts.append("⏸ En pause : " + ", ".join(fmt.esc(p.name) for p in paused))
        rows += [[(f"⏸ {p.name[:24]}", f"pjv:{p.id}")] for p in paused]
    return projects.cap_lines("\n\n".join(parts).split("\n")), rows


def rank_candidates(db: Database, project_id: str) -> list[Task]:
    """Tâches ouvertes du projet qui n'ont pas encore d'étape."""
    return [t for t in db.list_project_tasks(project_id) if t.status in projects.OPEN and not t.milestone_id]


def step_gaps(db: Database, project_id: str) -> list[Milestone]:
    """Étapes ouvertes (dans l'ordre) qui n'ont aucune tâche ouverte : celles où il reste du travail à découper."""
    busy = {t.milestone_id for t in db.list_project_tasks(project_id) if t.status in projects.OPEN and t.milestone_id}
    return [m for m in sorted(db.list_milestones(project_id), key=lambda m: m.position)
            if m.status == "open" and m.id not in busy]


def detail(db: Database, project_id: str, now: datetime) -> tuple[str, Buttons]:
    p = db.get_project(project_id)
    if p is None:
        return "❓ Projet introuvable.", []
    ms, tasks = db.list_milestones(p.id), db.list_project_tasks(p.id)
    prog = projects.progress(p, ms, tasks, now)
    text = projects.detail_card(p, ms, tasks, prog, now)
    if p.status == "done":
        return text, []
    first = [("➕ Étape", f"pm:add:{p.id}")]
    if prog.current is not None:
        first.insert(0, ("🏁 Étape franchie", f"pm:ok:{prog.current.id}"))
    toggle = ("⏸ Pause", f"pp:pause:{p.id}") if p.status == "active" else ("▶ Reprendre", f"pp:resume:{p.id}")
    rows = [first, [("✏️ Corriger", f"pp:fix:{p.id}")], [toggle, ("🏁 Terminer", f"pp:end:{p.id}")]]
    if p.status == "active" and prog.current is not None and rank_candidates(db, p.id):
        rows.insert(1, [("🗂 Ranger les tâches", f"pa:rng:{p.id}")])
    if p.status == "active" and step_gaps(db, p.id):   # le bouton n'existe que s'il y a quelque chose à créer
        rows.insert(1, [("🧩 Créer les tâches des étapes", f"pa:mk:{p.id}")])
    return text, rows


# --- actions (boutons `pjv:`, `pm:`, `pp:`) -------------------------------------------------

def act(db: Database, kind: str, rest: str, now: datetime) -> tuple[str, Buttons]:
    """`kind` = pjv | pm | pp ; `rest` = « ok:<étape> », « end:<projet> »… Les actions sont idempotentes."""
    if kind == "pjv":
        return detail(db, rest, now)
    sub, _, arg = rest.partition(":")
    if kind == "pm" and sub == "ok":
        m = db.get_milestone(arg)
        if m is None:
            return "❓ Étape introuvable.", []
        if m.status == "done":
            return "👌 Cette étape est déjà franchie, rien n'a changé.", []
        db.save_milestone(m.model_copy(update={"status": "done", "done_at": now}))
        db.touch_project(m.project_id, now)
        left = [x for x in db.list_milestones(m.project_id) if x.status == "open"]
        if left:
            return (f"🏁 Étape <b>{fmt.esc(m.title)}</b> franchie ! Prochaine : <i>{fmt.esc(left[0].title)}</i>.",
                    [[("📂 Voir le projet", f"pjv:{m.project_id}")]])
        return (f"🏁 Étape <b>{fmt.esc(m.title)}</b> franchie ! C'était la dernière : on termine le projet ?",
                [[("🏁 Terminer", f"pp:end:{m.project_id}"), ("Pas encore", "pm:no:-")]])
    if kind == "pm" and sub == "no":
        if arg == "-":   # après la dernière étape : il n'y aura pas de nouvelle question
            return "👌 D'accord, le projet reste ouvert : tu pourras le terminer depuis /projets.", []
        return "👌 D'accord, je te redemanderai quand d'autres tâches de cette étape seront terminées.", []
    if kind == "pp" and sub in ("pause", "resume", "end"):
        p = db.get_project(arg)
        if p is None:
            return "❓ Projet introuvable.", []
        status = {"pause": "paused", "resume": "active", "end": "done"}[sub]
        if p.status == "done":
            return "👌 Ce projet est terminé, rien n'a changé.", []
        if p.status == status:
            return "👌 Rien n'a changé.", []
        db.save_project(p.model_copy(update={"status": status}))
        db.touch_project(p.id, now)
        done = {"paused": "⏸ Projet mis en pause. Ses tâches restent rattachées, plus aucune proposition.",
                "active": "▶ Projet repris.", "done": "🏁 Projet terminé, bravo ! Sa catégorie est de nouveau libre."}
        return done[status], [[("📂 Voir le projet", f"pjv:{p.id}")]]
    return "❓ Action inconnue.", []


# --- étape prête à être franchie ------------------------------------------------------------

def after_task_done(db: Database, task_id: str, now: datetime) -> tuple[str, Buttons] | None:
    """À appeler quand une tâche vient d'être terminée. Met à jour l'activité du projet ; renvoie la demande
    « Étape franchie ? » (une seule fois par nombre de tâches faites) si l'étape n'a plus de tâche ouverte."""
    task = db.get_task(task_id)
    if task is None or not task.project_id:
        return None
    db.touch_project(task.project_id, now)
    p = db.get_project(task.project_id)
    if p is None or p.status != "active" or not task.milestone_id:
        return None
    m = db.get_milestone(task.milestone_id)
    if m is None or m.status != "open":
        return None
    tasks = db.list_project_tasks(p.id)
    if not projects.milestone_ready(m, tasks):
        return None
    done = sum(1 for t in tasks if t.milestone_id == m.id and t.status == "done")
    if not db.claim_notification(f"msdone:{m.id}:{done}"):
        return None
    return (f"🏁 Étape <b>{fmt.esc(m.title)}</b> de <b>{fmt.esc(p.name)}</b> : toutes ses tâches sont faites. "
            "Franchie ?", [[("Oui", f"pm:ok:{m.id}"), ("Pas encore", f"pm:no:{m.id}")]])
