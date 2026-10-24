"""Façade utilisée par bot.ports.CoachAdapter : tâches persistées + actions du scheduler.

`configure(db, scheduler)` doit être appelé une fois au démarrage (cf. run.py).
"""
from __future__ import annotations

import logging
from datetime import timedelta

from coach import dedupe, extract, fmt, project_assistant, project_service, project_watch
from coach.db import Database
from coach.models import Event, Task
from coach.scheduler import CoachScheduler

log = logging.getLogger("coach.store")

_db: Database | None = None
_sched: CoachScheduler | None = None


def configure(db: Database, scheduler: CoachScheduler) -> None:
    global _db, _sched, _interview
    _db, _sched = db, scheduler
    _interview = None


def _need() -> tuple[Database, CoachScheduler]:
    if _db is None or _sched is None:
        raise RuntimeError("coach.store non configuré (appeler configure())")
    return _db, _sched


def extract_items(text: str, source_id: str, sensitivity: str) -> tuple[list[Task], list[Event]]:
    """Extrait puis enregistre les tâches nouvelles (pending) et les rendez-vous nouveaux (« proposed ») ;
    ignore ce qui est déjà connu."""
    db, _ = _need()
    ctx = project_service.extraction_context(db)  # projets actifs : l'IA peut proposer un rattachement
    res = extract.extract_items(text, source_id, sensitivity, projects=ctx or None)  # type: ignore[arg-type]
    tasks = dedupe.dedupe_tasks(res.tasks, db.list_tasks())  # ni déjà connue, ni répétée, ni une démarche
    tasks = [project_service.attach_by_category(db, t) for t in tasks]  # préfixe = catégorie d'un projet
    for t in tasks:
        db.upsert_task(t)
        hint = res.hints.get(t.id)
        if hint and not t.project_id:
            project_service.propose(db, t, *hint)
        elif hint:  # déjà dans un projet par sa catégorie : l'IA peut suggérer l'étape
            project_service.propose_step(db, t, *hint)
    events: list[Event] = []
    known = db.all_events()
    for e in res.events:
        if dedupe.find_event(e, known + events) is not None:
            continue
        if db.add_event_if_new(e):
            if _in_calendar(e):  # déjà noté à la main dans Google Agenda : on ne le repropose pas
                db.set_event_status(e.id, "added")
                continue
            events.append(e)
    return tasks, events


def _in_calendar(ev: Event) -> bool:
    """Un rendez-vous à la même heure (à 30 min près) est déjà dans l'agenda : même titre approximatif ou non,
    c'est le même (mail de rappel d'un rendez-vous déjà noté)."""
    try:
        sched = _need()[1]
        found = sched.calendar.agenda(ev.start - timedelta(minutes=30), ev.end)
    except Exception:  # noqa: BLE001 - agenda injoignable : on propose quand même, l'ajout revérifiera
        return False
    return any(abs((s - ev.start).total_seconds()) <= 1800 for s, _e, _t in found)


def add_event(event_id: str) -> str:
    """Crée l'événement dans Google Calendar à l'heure exacte (aucun replacement)."""
    db, sched = _need()
    ev = db.get_event(event_id)
    if ev is None:
        raise KeyError(event_id)
    if ev.calendar_event_id:  # déjà ajouté : un second clic ne recrée rien
        return fmt.event_card(ev, "📅 <b>Déjà dans l'agenda</b>")
    already = [t for s, e, t in sched.calendar.agenda(ev.start, ev.end)
               if abs((s - ev.start).total_seconds()) < 3600 and dedupe.same_title(t, ev.title)]
    if already:  # le même rendez-vous est déjà dans Google Agenda (ajouté à la main, ou par un autre biais)
        db.set_event_status(ev.id, "added")
        return fmt.event_card(ev, "📅 <b>Déjà dans l'agenda</b>")
    clash = sched.calendar.agenda(ev.start, ev.end)  # cours, sport... (les blocs du coach lui cèdent la place)
    cal_id = sched.calendar.create_fixed_event(ev)
    db.set_event_status(ev.id, "added", cal_id)
    msg = fmt.event_card(ev, "📅 <b>Ajouté à l'agenda</b>")
    if clash:
        msg += "\n⚠️ Tu as déjà quelque chose sur ce créneau."
    moved = sched.resolve_conflicts()  # le rendez-vous est obligatoire : le coach se réorganise autour
    return "\n\n".join([msg, *moved])


def ignore_event(event_id: str) -> str:
    _need()[0].set_event_status(event_id, "ignored")
    return "🗑 Événement ignoré."


def list_open_tasks() -> list[Task]:
    return _need()[0].list_tasks("pending", "scheduled")


def agenda_context() -> str:
    return _need()[1].agenda_context()


def weekly_recap() -> str:
    return _need()[1].weekly_text()


def today_agenda() -> str:
    return _need()[1].briefing_text()


def mark_done(task_id: str) -> bool:
    db, sched = _need()
    with sched.lock:  # pas pendant qu'un placement déplace les blocs de cette tâche
        task = db.get_task(task_id)
        if task is None:
            return False
        for b in db.list_blocks(task_id, open_only=True):
            if b.calendar_event_id:
                sched.calendar.delete_event(b.calendar_event_id)
            db.set_block_outcome(b.id, "done")
        db.set_task_status(task_id, "done")
    sched.task_done(task_id)
    return True


def schedule(task_id: str) -> str:
    db, sched = _need()
    task = db.get_task(task_id)
    if task is None:
        raise KeyError(task_id)
    if task.status in ("scheduled", "done", "dropped") and db.list_blocks(task_id, open_only=True):
        return f"📌 <b>Déjà planifiée</b> : {fmt.esc(task.title)}"  # un second clic ne replace rien
    res = sched.schedule_tasks([task])
    if not res.blocks:
        return f"⚠️ Aucun créneau trouvé pour <b>{fmt.esc(task.title)}</b>."
    lines = [f"📌 <b>Planifiée</b> : {fmt.esc(task.title)}"]
    lines += [f"• {fmt.day_short(b.start)} {fmt.rng(b.start, b.end)}" for b in res.blocks]
    return "\n".join(lines)


def set_duration(task_id: str, minutes: int) -> str | None:
    """Fixe la durée ; si la tâche est déjà planifiée, ses blocs sont retirés et elle est replacée (texte du
    nouveau créneau), sinon rien d'autre ne change."""
    db, sched = _need()
    task = db.get_task(task_id)
    if task is None:
        raise KeyError(task_id)
    db.upsert_task(task.model_copy(update={"estimated_minutes": minutes}))
    blocks = db.list_blocks(task_id, open_only=True)
    if not blocks:
        return None
    for b in blocks:
        if b.calendar_event_id:
            sched.calendar.delete_event(b.calendar_event_id)
        db.delete_block(b.id)
    db.set_task_status(task_id, "pending")
    return schedule(task_id)


def ignore(task_id: str) -> None:
    _need()[1].drop_task(task_id)


def report_outcome(block_id: str, outcome: str) -> str:
    sched = _need()[1]
    if outcome == "done":
        return sched.mark_done(block_id)
    if outcome == "next_week":
        return sched.report(block_id, "rescheduled", "next_week")
    return sched.report(block_id, outcome)


def postpone_task(task_id: str, when: str = "tomorrow") -> str:
    """Report d'une tâche entière : `tomorrow` (au plus tôt demain) ou `next_week` (lundi prochain)."""
    return _need()[1].postpone_task(task_id, when)


def replan_all() -> str:
    """Refait tout le planning des blocs pas encore commencés."""
    return _need()[1].replan_all()


def resolve(action: str, task_id: str) -> str:
    """Boutons « Redécouper » / « Abandonner » envoyés par le scheduler."""
    return _need()[1].handle_callback(f"{action}:{task_id}")


def focus() -> tuple[str, list[list[tuple[str, str]]]]:
    """Le focus du moment : (texte HTML, boutons)."""
    return _need()[1].focus_card(now_mode=True)


def adjust_importance(task_id: str, up: bool) -> tuple[str, list[list[tuple[str, str]]]]:
    """Boutons ⬆ (`up`) / ⬇ : corrige l'importance d'une tâche, renvoie le focus recalculé."""
    return _need()[1].adjust_importance(task_id, up)


def start_now(task_id: str) -> str:
    """Bouton « Je m'y mets » d'une relance : replace la tâche au plus tôt."""
    return _need()[1].start_now(task_id)


def project_proposal(task_id: str) -> tuple[str, list[list[tuple[str, str]]]] | None:
    """(ligne « 📁 Projet … ? », boutons) à ajouter à la carte d'une tâche, ou None."""
    return project_service.proposal_card(_need()[0], task_id)


def project_pick(action: str, task_id: str, arg: str | None) -> tuple[str, list[list[tuple[str, str]]]]:
    """Boutons de la proposition de rattachement (`pjok`, `pjwho`, `pjset`, `pjms`, `pjno`)."""
    db, sched = _need()
    return project_service.pick(db, action, task_id, arg, sched.now())


# --- entretien de définition d'un projet ------------------------------------------------
_interview = None
_interview_cfg: dict = {"llm": None, "sensitivity": "private", "subjects": {}}


def configure_projects(llm, sensitivity: str = "private", subjects: dict[str, str] | None = None) -> None:
    """Branche l'IA (par ex. `router.complete`) et la table des matières Notion sur l'entretien (cf. run.py)."""
    global _interview
    _interview_cfg.update(llm=llm, sensitivity=sensitivity, subjects=subjects or {})
    _interview = None


def _iv():
    global _interview
    if _interview is None:
        from coach.project_interview import Interview
        _interview = Interview(_need()[0], _interview_cfg["llm"], _interview_cfg["sensitivity"],
                               _interview_cfg["subjects"], _need()[1].now)
    return _interview


def interview_active() -> bool:
    return _iv().active()


def interview_start() -> list[tuple[str, list[list[tuple[str, str]]]]]:
    return _iv().start()


def interview_text(text: str) -> list[tuple[str, list[list[tuple[str, str]]]]]:
    return _iv().on_text(text)


def interview_callback(rest: str) -> list[tuple[str, list[list[tuple[str, str]]]]]:
    return _iv().on_callback(rest)


def interview_cancel() -> list[tuple[str, list[list[tuple[str, str]]]]]:
    return _iv().cancel()


def projects_overview() -> tuple[str, list[list[tuple[str, str]]]]:
    db, sched = _need()
    return project_service.overview(db, sched.now())


def project_action(kind: str, rest: str) -> tuple[str, list[list[tuple[str, str]]]]:
    """Boutons `pjv:` (détail), `pm:` (étapes), `pp:` (pause, reprise, fin, correction)."""
    if kind == "pm" and rest.startswith("add:"):
        return _iv().begin_add_milestone(rest[4:])[0]
    if kind == "pp" and rest.startswith("fix:"):
        return _iv().begin_fix_project(rest[4:])[0]
    db, sched = _need()
    if kind == "pm" and rest.startswith("ok:"):
        step = db.get_milestone(rest[3:])
        was_open = step is not None and step.status == "open"
        text, buttons = project_service.act(db, kind, rest, sched.now())
        if was_open:  # étape franchie à l'instant : l'assistant propose la suite (jamais bloquant)
            try:
                more = project_assistant.after_milestone_crossed(
                    db, _interview_cfg["llm"], _interview_cfg["sensitivity"], step.project_id, sched.now())
            except Exception:  # noqa: BLE001
                log.warning("assistant de projet : suggestions impossibles", exc_info=True)
                more = None
            if more:
                return f"{text}\n\n{more[0]}", more[1] + buttons
        return text, buttons
    return project_service.act(db, kind, rest, sched.now())


# --- assistant de projet (suggestions, rangement) ---------------------------------------------

def _plan_task(task_id: str) -> list:
    """Place la tâche avec le planificateur (comme le bouton « Planifier ») ; renvoie les blocs ([] : aucun créneau)."""
    db, sched = _need()
    task = db.get_task(task_id)
    if task is None:
        return []
    placed = db.list_blocks(task_id, open_only=True)
    if placed:
        return placed   # déjà placée (double appui) : on ne replace rien, comme `schedule`
    return list(sched.schedule_tasks([task]).blocks)


def assistant_action(kind: str, rest: str) -> tuple[str, list[list[tuple[str, str]]]]:
    """Boutons `ps:` (suggestions de tâches) et `pa:` (rangement dans les étapes)."""
    db, sched = _need()
    action, _, arg = rest.partition(":")
    if kind == "ps" and action == "go":   # hors du verrou des clics : l'appel IA peut être lent
        return project_assistant.go(db, _interview_cfg["llm"], _interview_cfg["sensitivity"], arg, sched.now())
    if kind == "ps":
        return project_assistant.click(db, action, arg, sched.now(), plan=_plan_task)
    if kind == "pa" and action == "mk":   # hors du verrou des clics : l'appel IA peut être lent
        return project_assistant.fill_steps(db, _interview_cfg["llm"], _interview_cfg["sensitivity"], arg, sched.now())
    if kind == "pa" and action == "rng":
        return project_assistant.rank_batch(db, _interview_cfg["llm"], _interview_cfg["sensitivity"], arg, sched.now())
    if kind == "pa" and action in ("ok", "no", "all", "none"):
        return project_assistant.rank_click(db, action, arg, sched.now())
    return "❓ Action inconnue.", []


def projects_idle_check(notify) -> int:
    """Tâche planifiée (toutes les 15 min) : propose la suite des projets actifs sans tâche ouverte."""
    db, sched = _need()
    now = sched.now()
    hours = sched.rules.reminder_window(now.date())
    if not (hours.start <= now.time() < hours.end):
        return 0   # pas de proposition la nuit : elle attendrait l'utilisateur au réveil (les clics, eux, restent libres)
    return project_assistant.idle_check(db, _interview_cfg["llm"], _interview_cfg["sensitivity"], notify, sched.now())


def projects_watch(notify) -> int:
    """Tâche planifiée (toutes les 30 min) : prévient d'un projet actif qui stagne ou dont le rendu approche."""
    db, sched = _need()
    now = sched.now()
    hours = sched.rules.reminder_window(now.date())
    if not (hours.start <= now.time() < hours.end):
        return 0   # pas d'alerte la nuit : elle attendrait l'utilisateur au réveil
    return project_watch.watch(db, notify, now)
