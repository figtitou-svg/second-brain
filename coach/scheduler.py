"""Orchestration : placement dans l'agenda, relances, replacement, briefing.

`notify(text, buttons)` est injecté (le bot Telegram fournit l'implémentation).
`buttons` = liste de (libellé, callback_data) ou None. Les callback_data à renvoyer
à `CoachScheduler.handle_callback` ont la forme `<action>:<id>` :
  done:<block_id>  notfinished:<block_id>  postpone:<block_id>  nextweek:<block_id>
  split:<task_id>  drop:<task_id>
"""
from __future__ import annotations

import functools
import logging
import threading
from datetime import date, datetime, timedelta
from typing import Callable
from zoneinfo import ZoneInfo

from coach import focus, fmt, project_service
from coach.calendar_client import STYLE_COLOR, CalendarClient, event_title
from coach.db import Database
from coach.models import Block, Task, new_id
from coach.planner import PlanResult, plan_detailed
from coach.rules import Rules

log = logging.getLogger("coach.scheduler")

Button = tuple[str, str]
Notify = Callable[[str, "list[Button] | None"], None]

END_WINDOW = timedelta(hours=6)       # au-delà, on ne relance plus "C'est fait ?" : le bloc est manqué et replacé
BRIEFING_WINDOW = timedelta(hours=3)  # briefing ignoré si le serveur était éteint plus longtemps
NUDGE_MIN_OFF_FOCUS_MIN = 90          # minutes terminées hors focus avant de relancer
NUDGE_MAX_PER_DAY = 2
NUDGE_URGENT = timedelta(hours=48)    # échéance sous ce délai = urgent
NUDGE_SOON_BLOCK = timedelta(hours=2)  # un bloc de la tâche démarre bientôt : pas de relance
NUDGE_QUIET = timedelta(minutes=15)    # jamais dans ce délai autour d'un début ou d'une fin de bloc / rendez-vous


ALREADY_HANDLED = "👌 Ce créneau a déjà été traité, rien n'a changé."


class CoachScheduler:
    def __init__(
        self,
        db: Database,
        calendar: CalendarClient,
        rules: Rules,
        notify: Notify,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self.db, self.calendar, self.rules, self.notify = db, calendar, rules, notify
        self.tz = ZoneInfo(rules.timezone)
        self._now_fn = now_fn or (lambda: datetime.now(self.tz))
        # bot (boutons), job coach et job Notion tournent dans des threads différents : un seul placement à la fois,
        # sinon deux calculs voient le même agenda libre et posent deux blocs au même endroit
        self.lock = threading.RLock()
        # reformulation des raisons du focus par l'IA : branchée par run.py (None = raisons calculées seulement)
        self.explain_fn: Callable[..., str] | None = None
        self.explain_sensitivity = "private"
        # appelé dès qu'une tâche passe à « terminée » (ex. : la cocher aussi dans Notion tout de suite)
        self.on_task_done: Callable[[str], None] | None = None

    def task_done(self, task_id: str) -> None:
        if self.on_task_done:
            try:
                self.on_task_done(task_id)
            except Exception:  # noqa: BLE001 - la synchro périodique rattrapera
                log.warning("on_task_done en échec pour %s", task_id, exc_info=True)
        try:  # projets : activité mise à jour, et « étape franchie ? » si c'était la dernière tâche de l'étape
            ask = project_service.after_task_done(self.db, task_id, self.now())
            if ask:
                self.notify(ask[0], ask[1])
        except Exception:  # noqa: BLE001 - jamais bloquant pour la complétion de la tâche
            log.warning("projets : demande d'étape en échec pour %s", task_id, exc_info=True)

    def now(self) -> datetime:
        return self._now_fn().astimezone(self.tz)

    # --- placement ---------------------------------------------------------
    def schedule_tasks(self, tasks: list[Task]) -> PlanResult:
        """Enregistre les tâches, les planifie et crée les événements."""
        for t in tasks:
            self.db.upsert_task(t)
        return self._place(tasks)

    def _not_before(self, when: str, now: datetime | None = None) -> datetime:
        """Début de la période où le report peut tomber : demain 00:00, ou lundi prochain 00:00."""
        now = now or self.now()
        today = datetime.combine(now.date(), datetime.min.time(), tzinfo=self.tz)
        if when == "next_week":
            return today + timedelta(days=7 - now.weekday())
        if when not in ("tomorrow", ""):  # date libre AAAA-MM-JJ
            target = datetime.combine(date.fromisoformat(when), datetime.min.time(), tzinfo=self.tz)
            return max(target, today + timedelta(days=1))
        return today + timedelta(days=1)

    def _movable(self, tasks: list[Task], now: datetime, broad: bool) -> dict[str, list[Block]]:
        """Blocs pas encore commencés d'autres tâches qu'on accepte de déplacer pour faire de la place :
        d'abord ceux dont l'échéance est plus lointaine, puis (`broad`) tous, replacés par priorité / échéance."""
        mine = {t.id for t in tasks}
        dues = [t.due for t in tasks if t.due]
        out: dict[str, list[Block]] = {}
        for b in self.db.list_blocks(open_only=True):
            if b.task_id in mine or b.start <= now:
                continue
            other = self.db.get_task(b.task_id)
            if other is None or other.status in ("done", "dropped"):
                continue
            if not broad and not (dues and other.due and other.due > min(dues)):
                continue
            out.setdefault(other.id, []).append(b)
        return out

    def _plan_with(self, tasks: list[Task], victims: dict[str, list[Block]], now: datetime,
                   not_before: datetime | None, busy_all: list | None = None,
                   free: list | None = None) -> tuple[PlanResult, list[Task]]:
        """Calcul pur : replanifie `tasks` + les tâches des blocs `victims` (leurs créneaux sont libérés)."""
        extra = [self.db.get_task(tid).model_copy(update={"estimated_minutes": sum(b.minutes for b in blks)})
                 for tid, blks in victims.items()]
        every = tasks + extra
        mine = {t.id for t in tasks}
        base = max(now, not_before) if not_before else now
        horizon = max((t.due for t in every if t.due), default=base + timedelta(days=7))
        horizon = max(horizon, base + timedelta(days=7)) + timedelta(days=1)
        freed = {(b.start, b.end) for blks in victims.values() for b in blks} | set(free or [])
        if busy_all is None:
            busy_all = self.calendar.freebusy(now, horizon)
        busy = [iv for iv in busy_all if iv not in freed]
        gone = {b.id for blks in victims.values() for b in blks}
        used: dict = {}
        for ob in self.db.list_blocks(open_only=True):  # plafond quotidien : compter les blocs déjà placés
            if ob.id not in gone:
                d = ob.start.astimezone(self.tz).date()
                used[d] = used.get(d, 0) + ob.minutes
        planned = every
        if not_before:  # l'utilisateur a choisi de reporter : si rien ne tient avant l'échéance, on dépasse l'échéance
            planned = [t.model_copy(update={"due": not_before + timedelta(days=7)})
                       if t.id in mine and t.due and t.due < not_before + timedelta(days=7) else t for t in every]
        res = plan_detailed(planned, busy, self.rules, now, used, not_before, mine if not_before else None)
        return res, every

    def _place(self, tasks: list[Task], reuse_event: dict[str, str] | None = None,
               not_before: datetime | None = None, free: list | None = None) -> PlanResult:
        """`free` : créneaux des anciens blocs de ces tâches (déjà retirés de la base), à ne plus compter comme occupés."""
        now = self.now()
        dues = [t.due for t in tasks if t.due] + [t.due for t in self.db.list_tasks("pending", "scheduled") if t.due]
        far = max(dues + [(not_before or now) + timedelta(days=7)]) + timedelta(days=9)
        busy_all = self.calendar.freebusy(now, far)
        # on déplace d'abord le moins possible : rien, puis les blocs à échéance plus lointaine, puis tous
        # les blocs pas commencés ; on s'arrête au premier niveau où tout tient avant les échéances
        levels = [{}, self._movable(tasks, now, False), self._movable(tasks, now, True)]
        for victims in levels:
            result, every = self._plan_with(tasks, victims, now, not_before, busy_all, free)
            if not result.unplaced:
                break
        return self._apply(result, every, victims, reuse_event)

    def replan_all(self) -> str:
        """Replace d'un coup tous les blocs pas encore commencés (priorité puis échéance), sans question."""
        now = self.now()
        victims = self._movable([], now, True)
        if not victims:
            return "Rien à replacer."
        old = {b.id: (b.start, b.end) for blks in victims.values() for b in blks}
        result, every = self._plan_with([], victims, now, None)
        self._apply(result, every, victims, None)
        moved = sum(1 for b in result.blocks if (b.start, b.end) not in old.values())
        msg = f"🔄 <b>Planning refait</b> : {len(result.blocks)} blocs, {moved} déplacés."
        if result.unplaced:
            msg += f"\n⚠️ {len(result.unplaced)} tâche(s) sans assez de place avant l'échéance."
        return msg

    def _apply(self, result: PlanResult, every: list[Task], victims: dict[str, list[Block]],
               reuse_event: dict[str, str] | None) -> PlanResult:
        reuse = dict(reuse_event or {})
        for tid, blks in victims.items():
            for i, b in enumerate(sorted(blks, key=lambda x: x.start)):
                if i == 0 and b.calendar_event_id and tid not in reuse:
                    reuse[tid] = b.calendar_event_id  # un événement est déplacé, les autres supprimés
                elif b.calendar_event_id:
                    self.calendar.delete_event(b.calendar_event_id)
                self.db.delete_block(b.id)
        by_id = {t.id: t for t in every}
        levels = {r.task.id: r.level for r in self.ranking()}  # style des nouveaux événements
        for blk in result.blocks:
            task = by_id[blk.task_id]
            old = reuse.pop(blk.task_id, None)
            if old:
                self.calendar.move_event(old, blk.start, blk.end)
                blk.calendar_event_id = old
            else:
                blk.style = levels.get(task.id)
                blk.calendar_event_id = self.calendar.create_event(blk, task, blk.style)
            self.db.add_block(blk)
            self.db.set_task_status(task.id, "scheduled")
        for tid, minutes in result.unplaced.items():
            t = by_id[tid]
            self.notify(
                f"⚠️ <b>Pas assez de place</b>\nImpossible de placer {fmt.dur(minutes)} pour "
                f"<b>{fmt.esc(t.title)}</b> avant l'échéance.\nOn redécoupe ou on abandonne ?",
                [("Redécouper", f"split:{tid}"), ("Abandonner", f"drop:{tid}")],
            )
        for orphan in reuse.values():  # événement non réutilisé (rien de replacé)
            self.calendar.delete_event(orphan)
        return result

    # --- boucle périodique ---------------------------------------------------
    def tick(self, now: datetime | None = None) -> None:
        now = (now or self.now()).astimezone(self.tz)
        lead = timedelta(minutes=self.rules.reminder_lead_minutes)
        for blk in self.db.list_blocks(open_only=True):
            task = self.db.get_task(blk.task_id)
            if task is None or task.status in ("done", "dropped"):
                continue
            if blk.start - lead <= now < blk.start and self.db.claim_notification(f"pre:{blk.id}"):
                self.notify(f"⏰ <b>Dans {self.rules.reminder_lead_minutes} min</b>\n{fmt.esc(task.title)}"
                            f"\n{fmt.rng(blk.start, blk.end)}", None)
            if blk.end <= now < blk.end + END_WINDOW and self.db.claim_notification(f"end:{blk.id}"):
                self.notify(
                    f"🔔 <b>C'est fait ?</b>\n{fmt.esc(task.title)}",
                    [("Oui", f"done:{blk.id}"), ("Pas fini", f"notfinished:{blk.id}"),
                     ("Reporter", f"postpone:{blk.id}"), ("Semaine pro.", f"nextweek:{blk.id}")],
                )
        for ev in self.db.list_events("added"):
            if ev.start - lead <= now < ev.start and self.db.claim_notification(f"evpre:{ev.id}"):
                where = f"\n📍 {fmt.esc(ev.location)}" if ev.location else ""
                self.notify(f"⏰ <b>Dans {self.rules.reminder_lead_minutes} min</b>\n{fmt.esc(ev.title)}"
                            f"\n{fmt.rng(ev.start, ev.end)}{where}", None)
        self._replan_missed(now)
        try:
            msgs = self.resolve_conflicts(now)
        except Exception:  # noqa: BLE001 - agenda injoignable : les rappels ci-dessus ont déjà été envoyés
            log.warning("détection des conflits en échec", exc_info=True)
            msgs = []
        if msgs:
            self.notify("\n\n".join(msgs), None)
        try:  # couleurs de l'agenda : ne bloque jamais le reste
            self.sync_styles(now)
        except Exception:  # noqa: BLE001
            log.warning("synchronisation des couleurs en échec", exc_info=True)
        try:
            self._nudge(now)
        except Exception:  # noqa: BLE001
            log.warning("relances en échec", exc_info=True)
        self._briefing(now)
        self._weekly(now)

    def _replan_missed(self, now: datetime) -> None:
        """Bloc resté sans réponse après END_WINDOW : marqué « missed » et replacé dès que possible."""
        msgs = []
        for blk in self.db.list_blocks(open_only=True):
            task = self.db.get_task(blk.task_id)
            if task is None or task.status in ("done", "dropped") or now < blk.end + END_WINDOW:
                continue
            msgs.append(f"⌛ <b>Pas de réponse</b> pour {fmt.esc(task.title)}\n{self.report(blk.id, 'missed')}")
        if msgs:
            self.notify("\n\n".join(msgs), None)

    def _nudge(self, now: datetime) -> None:
        """Relance : du temps passé hors focus alors qu'une priorité urgente n'a pas démarré (garde-fous : spec §8)."""
        hours = self.rules.reminder_window(now.date())
        if not (hours.start <= now.time() < hours.end):
            return
        day = now.date()
        day_start = datetime.combine(day, datetime.min.time(), tzinfo=self.tz)
        blocks = self.db.list_blocks()
        done_today = [b for b in blocks if b.outcome == "done" and day_start <= b.end <= now]
        top = self.ranking(now)[:focus.FOCUS_SIZE]
        top_ids = {r.task.id for r in top}
        off_minutes = sum(b.minutes for b in done_today if b.task_id not in top_ids)
        if off_minutes < NUDGE_MIN_OFF_FOCUS_MIN:
            return
        open_blocks = [b for b in blocks if b.outcome is None]
        edges = [x for b in open_blocks for x in (b.start, b.end)]
        edges += [x for e in self.db.list_events("added") for x in (e.start, e.end)]
        if any(abs(now - x) < NUDGE_QUIET for x in edges):
            return
        worked = {b.task_id for b in done_today}
        for r in top:
            t = r.task
            urgent = r.factors["on_day"] or (t.due is not None and t.due - now <= NUDGE_URGENT)
            if t.id in worked or not urgent:
                continue
            if any(b.task_id == t.id and b.start <= now + NUDGE_SOON_BLOCK and b.end > now for b in open_blocks):
                continue
            if self.db.has_notification(f"nudge:{day}:{t.id}"):
                continue
            slot = next((n for n in range(1, NUDGE_MAX_PER_DAY + 1)
                         if self.db.claim_notification(f"nudgecount:{day}:{n}")), None)
            if slot is None:  # déjà 2 relances aujourd'hui
                return
            self.db.claim_notification(f"nudge:{day}:{t.id}")
            when = "pour aujourd'hui" if r.factors["on_day"] else f"avant {fmt.when(t.due)}"
            self.notify(f"🎯 Tu as fait {fmt.dur(off_minutes)} de tâches hors focus, mais "
                        f"<b>{fmt.esc(t.title)}</b> ({when}) n'est pas commencé.",
                        [[("Je m'y mets", f"nudgego:{t.id}"), ("Pas maintenant", f"nudgeno:{t.id}")]])
            return

    def _fixed_busy(self, start: datetime, end: datetime) -> list[tuple[datetime, datetime, str]]:
        """Ce que l'utilisateur impose : événements des agendas (hors blocs du coach) et rendez-vous ajoutés par le bot."""
        items = list(self.calendar.agenda(start, end)) if hasattr(self.calendar, "agenda") else []
        items += [(e.start, e.end, e.title) for e in self.db.list_events("added") if e.end > start and e.start < end]
        return items

    def resolve_conflicts(self, now: datetime | None = None) -> list[str]:
        """Un événement ajouté par l'utilisateur est prioritaire : les blocs du coach qui le chevauchent
        (et qui n'ont pas commencé) sont replacés ailleurs. Retourne un message par bloc déplacé."""
        now = (now or self.now()).astimezone(self.tz)
        blocks = sorted((b for b in self.db.list_blocks(open_only=True) if b.start > now), key=lambda b: b.start)
        if not blocks:
            return []
        fixed = self._fixed_busy(blocks[0].start, max(b.end for b in blocks))
        msgs: list[str] = []
        for blk in blocks:
            hit = next(((s, e, t) for s, e, t in fixed if blk.start < e and blk.end > s), None)
            task = self.db.get_task(blk.task_id)
            if hit is None or task is None or task.status in ("done", "dropped"):
                continue
            self.db.delete_block(blk.id)
            reuse = {task.id: blk.calendar_event_id} if blk.calendar_event_id else None
            res = self._place([task.model_copy(update={"estimated_minutes": blk.minutes})], reuse)
            nxt = next(iter(res.blocks), None)
            what = f"« {fmt.esc(hit[2])} »" if hit[2] else "Un nouvel événement"
            if nxt:
                msgs.append(f"🔀 {what} passe avant : <b>{fmt.esc(task.title)}</b> déplacée\n{fmt.when(nxt.start)}")
            else:
                if not self.db.list_blocks(task.id, open_only=True):
                    self.db.set_task_status(task.id, "pending")
                msgs.append(f"⚠️ {what} passe avant, mais je n'ai plus de créneau pour <b>{fmt.esc(task.title)}</b>.")
        return msgs

    def _weekly(self, now: datetime) -> None:
        """Récap hebdomadaire : le dimanche à `weekly_recap_time` (rattrapé jusqu'à 3 h après)."""
        if now.weekday() != 6:
            return
        at = datetime.combine(now.date(), self.rules.weekly_recap_time, tzinfo=self.tz)
        if not (at <= now < at + BRIEFING_WINDOW):
            return
        year, week, _ = now.isocalendar()
        if self.db.claim_notification(f"week:{year}-{week}"):
            self.notify(self.weekly_text(now), None)

    def _briefing(self, now: datetime) -> None:
        at = datetime.combine(now.date(), self.rules.briefing_time, tzinfo=self.tz)
        if not (at <= now < at + BRIEFING_WINDOW):
            return
        if not self.db.claim_notification(f"brief:{now.date().isoformat()}"):
            return
        self.notify(self.briefing_text(now), self.focus_buttons(now) or None)

    def briefing_text(self, now: datetime | None = None) -> str:
        now = (now or self.now()).astimezone(self.tz)
        day_start = datetime.combine(now.date(), datetime.min.time(), tzinfo=self.tz)
        day_end = day_start + timedelta(days=1)
        parts = [f"☀️ <b>Programme du {fmt.day_long(now)}</b>"]
        if hasattr(self.calendar, "agenda"):
            agenda = [(s, e, t) for s, e, t in self.calendar.agenda(day_start, day_end)]
        else:
            agenda = [(s, e, "") for s, e in self.calendar.freebusy(day_start, day_end)]
        focus_blocks = [b for b in self.db.list_blocks(open_only=True) if day_start <= b.start < day_end]
        own = {(b.start, b.end) for b in focus_blocks}
        agenda = [(s, e, t) for s, e, t in agenda if (s, e) not in own]
        if agenda:
            parts.append("📚 <b>Agenda</b>\n" + "\n".join(
                f"• {fmt.rng(s, e)} {fmt.esc(t)}".rstrip() for s, e, t in sorted(agenda)))
        evs = [e for e in self.db.list_events("added") if day_start <= e.start < day_end]
        if evs:
            parts.append("📅 <b>Rendez-vous</b>\n" + "\n".join(
                f"• {fmt.rng(e.start, e.end)} {fmt.esc(e.title)}" + (f" · 📍 {fmt.esc(e.location)}" if e.location else "")
                for e in evs))
        if focus_blocks:
            lines = []
            for b in focus_blocks:
                t = self.db.get_task(b.task_id)
                lines.append(f"• {fmt.rng(b.start, b.end)} {fmt.esc(t.title if t else b.task_id)}")
            parts.append("🎯 <b>Créneaux focus</b>\n" + "\n".join(lines))
        soon = sorted((t for t in self.db.list_tasks("pending", "scheduled")
                       if t.due and t.due <= now + timedelta(hours=48)), key=lambda t: t.due)  # type: ignore[arg-type,return-value]
        if soon:
            parts.append("⚠️ <b>Échéances sous 48 h</b>\n" + "\n".join(
                f"• {fmt.esc(t.title)} (avant {fmt.when(t.due)})" for t in soon))  # type: ignore[arg-type]
        ranked = self.ranking(now)  # le focus du jour, juste sous le titre (« journée libre » sans tâche)
        why = focus.reasons(ranked[:focus.FOCUS_SIZE], self.explain_fn, self.db, self.explain_sensitivity) if ranked else {}
        parts.insert(1, focus.render(ranked, why, now))
        return "\n\n".join(parts)

    # --- focus du jour ---------------------------------------------------------
    def ranking(self, now: datetime | None = None) -> list[focus.Ranked]:
        """Tâches ouvertes classées par importance (calcul en base, sans réseau)."""
        now = (now or self.now()).astimezone(self.tz)
        return focus.rank_tasks(self.db.list_tasks("pending", "scheduled"), self.db.done_minutes(), now,
                                self.db.get_weights(), self.rules.max_focus_minutes_per_day,
                                self.rules.min_block_minutes)

    def focus_buttons(self, now: datetime | None = None) -> list[list[tuple[str, str]]]:
        return focus.focus_buttons(self.ranking(now))

    def _free_before_next(self, now: datetime) -> str | None:
        try:
            items = sorted(self._fixed_busy(now, now + timedelta(hours=12)))
        except Exception:  # noqa: BLE001 - agenda injoignable : cette ligne est facultative
            return None
        for start, end, title in items:
            label = f"« {fmt.esc(title)} »" if title else "ton prochain événement"
            if start <= now < end:
                return f"📍 En ce moment : {label} jusqu'à {fmt.hm(end)}"
            if start > now:
                mins = int((start - now).total_seconds() // 60)
                return f"⏳ Tu as {fmt.dur(mins)} avant {label}" if mins >= 10 else None
        return None

    def focus_card(self, now: datetime | None = None, now_mode: bool = False,
                   note: str | None = None) -> tuple[str, list[list[tuple[str, str]]]]:
        """(texte HTML, boutons) du focus ; `now_mode` : « à faire maintenant » + temps libre avant l'agenda."""
        now = (now or self.now()).astimezone(self.tz)
        ranked = self.ranking(now)
        why = focus.reasons(ranked[:focus.FOCUS_SIZE], self.explain_fn, self.db, self.explain_sensitivity)
        extra = self._free_before_next(now) if now_mode and ranked else None
        text = focus.render(ranked, why, now, now_mode=now_mode, extra_line=extra)
        return (f"{note}\n\n{text}" if note else text), focus.focus_buttons(ranked)

    def adjust_importance(self, task_id: str, up: bool) -> tuple[str, list[list[tuple[str, str]]]]:
        """Boutons ⬆ / ⬇ : décale la tâche et pousse (ou baisse) le poids de sa matière, puis recalcule le focus."""
        task = self.db.get_task(task_id)
        if task is None or task.status in ("done", "dropped"):
            return self.focus_card(now_mode=True, note="👌 Cette tâche est déjà traitée, rien n'a changé.")
        subject = focus.subject_of(task.title)
        weight = self.db.get_weights().get(subject, 1.0) if subject else 1.0
        boost, new_weight = focus.adjusted(task.boost, weight, up)
        self.db.set_task_boost(task.id, boost)
        if subject:
            self.db.set_weight(subject, new_weight)
        self.sync_styles()  # l'étoile et les couleurs suivent tout de suite
        return self.focus_card(now_mode=True)

    def sync_styles(self, now: datetime | None = None) -> None:
        """Aligne titre et couleur des blocs ouverts sur l'importance actuelle ; ne patche que ce qui a changé.
        Une erreur de l'API agenda est journalisée : le style n'est pas mémorisé, le prochain passage réessaie."""
        level_of = {r.task.id: r.level for r in self.ranking(now)}
        for blk in self.db.list_blocks(open_only=True):
            level = level_of.get(blk.task_id)
            if level is None or not blk.calendar_event_id or blk.style == level:
                continue
            task = self.db.get_task(blk.task_id)
            try:
                self.calendar.recolor_event(blk.calendar_event_id, event_title(task, level), STYLE_COLOR[level])
            except Exception:  # noqa: BLE001
                log.warning("couleur non mise à jour pour le bloc %s", blk.id, exc_info=True)
                continue
            self.db.set_block_style(blk.id, level)

    def agenda_context(self, days: int = 7, now: datetime | None = None) -> str:
        """Texte brut (pour un LLM) : date/heure actuelles, agenda, blocs, rendez-vous et échéances à venir."""
        now = (now or self.now()).astimezone(self.tz)
        today = datetime.combine(now.date(), datetime.min.time(), tzinfo=self.tz)
        end = today + timedelta(days=days)
        items: list[tuple[datetime, str]] = []
        if hasattr(self.calendar, "agenda"):
            for s, e, t in self.calendar.agenda(today, end):
                items.append((s, f"{fmt.rng(s, e)} {t or 'occupé'} (agenda)"))
        for b in self.db.list_blocks(open_only=True):
            if today <= b.start < end:
                task = self.db.get_task(b.task_id)
                items.append((b.start, f"{fmt.rng(b.start, b.end)} travail : {task.title if task else b.task_id}"))
        for e in self.db.list_events("added"):
            if today <= e.start < end:
                where = f" à {e.location}" if e.location else ""
                items.append((e.start, f"{fmt.rng(e.start, e.end)} rendez-vous : {e.title}{where}"))
        lines = [f"Date et heure actuelles : {fmt.day_long(now)} {now.year}, {fmt.hm(now)} (Europe/Paris).",
                 f"Programme des {days} prochains jours :"]
        by_day: dict = {}
        for s, text in sorted(items):
            by_day.setdefault(s.date(), []).append(text)
        for d in sorted(by_day):
            lines.append(f"- {fmt.day_long(datetime.combine(d, datetime.min.time()))} :")
            lines += [f"    {x}" for x in by_day[d]]
        if not by_day:
            lines.append("- rien de prévu dans l'agenda.")
        due = sorted((t for t in self.db.list_tasks("pending", "scheduled") if t.due), key=lambda t: t.due)  # type: ignore[arg-type,return-value]
        if due:
            lines.append("Échéances en cours :")
            lines += [f"- {t.title} : pour le {fmt.day_long(t.due)}" + (" (EN RETARD)" if t.due < now else "")  # type: ignore[arg-type,operator]
                      for t in due[:25]]
        return "\n".join(lines)

    def weekly_text(self, now: datetime | None = None) -> str:
        """Échéances et rendez-vous des 7 prochains jours, retards, et aperçu plus tard."""
        now = (now or self.now()).astimezone(self.tz)
        today = datetime.combine(now.date(), datetime.min.time(), tzinfo=self.tz)
        horizon = today + timedelta(days=7)
        tasks = [t for t in self.db.list_tasks("pending", "scheduled") if t.due]
        parts = ["📆 <b>Récap de la semaine</b>"]
        late = sorted((t for t in tasks if t.due < now), key=lambda t: t.due)  # type: ignore[arg-type,return-value]
        if late:
            parts.append("🚨 <b>En retard</b>\n" + "\n".join(
                f"• {fmt.esc(t.title)} (était pour le {fmt.day_short(t.due)})" for t in late))  # type: ignore[arg-type]
        days: dict = {}
        for t in tasks:
            if now <= t.due < horizon:  # type: ignore[operator]
                days.setdefault(t.due.date(), []).append(  # type: ignore[union-attr]
                    (t.due, f"• {fmt.esc(t.title)} · ⏱ {fmt.dur(t.estimated_minutes)}"
                     + (" · 📌 planifiée" if t.status == "scheduled" else "")))
        for e in self.db.list_events("added"):
            if now <= e.start < horizon:
                days.setdefault(e.start.date(), []).append(
                    (e.start, f"• 📅 {fmt.hm(e.start)} {fmt.esc(e.title)}"
                     + (f" · 📍 {fmt.esc(e.location)}" if e.location else "")))
        for d in sorted(days):
            parts.append(f"<b>{fmt.day_long(datetime.combine(d, datetime.min.time()))}</b>\n"
                         + "\n".join(line for _, line in sorted(days[d], key=lambda x: x[0])))
        later = sorted((t for t in tasks if t.due >= horizon), key=lambda t: t.due)  # type: ignore[arg-type,return-value]
        if later:
            nxt = later[0]
            parts.append(f"🔭 <b>Plus tard</b> : {len(later)} échéance(s), la prochaine : "
                         f"{fmt.esc(nxt.title)} ({fmt.day_short(nxt.due)})")  # type: ignore[arg-type]
        if len(parts) == 1:
            parts.append("Rien d'ici 7 jours 🎉")
        return "\n\n".join(parts)

    # --- réponses aux boutons --------------------------------------------------
    def handle_callback(self, data: str) -> str:
        action, _, ident = data.partition(":")
        if action == "done":
            return self.mark_done(ident)
        if action in ("notfinished", "postpone", "nextweek"):
            return self.report(ident, "not_finished" if action == "notfinished" else "rescheduled",
                               "next_week" if action == "nextweek" else "tomorrow")
        if action == "split":
            return self.split_task(ident)
        if action == "drop":
            return self.drop_task(ident)
        raise ValueError(f"callback inconnu : {data}")

    def mark_done(self, block_id: str) -> str:
        blk = self._block(block_id)
        if blk.outcome is not None:  # clic tardif ou double clic : le bloc a déjà été traité
            return ALREADY_HANDLED
        self.db.set_block_outcome(blk.id, "done")
        task = self._task(blk.task_id)
        if not self.db.list_blocks(task.id, open_only=True):
            self.db.set_task_status(task.id, "done")
            self.task_done(task.id)
            return f"✅ <b>{fmt.esc(task.title)}</b> est terminée. Bravo !"
        return f"👍 Bloc terminé pour <b>{fmt.esc(task.title)}</b>."

    def report(self, block_id: str, outcome: str, when: str = "tomorrow") -> str:
        """`Pas fini` (not_finished) et bloc manqué (missed) : replacé dès que possible. `Reporter` (rescheduled) : au plus tôt demain
        (`when="tomorrow"`) ou lundi prochain (`when="next_week"`), jamais dans la journée."""
        blk = self._block(block_id)
        if blk.outcome is not None:
            return ALREADY_HANDLED
        task = self._task(blk.task_id)
        self.db.set_block_outcome(blk.id, outcome)
        count = self.db.increment_reschedule(task.id)
        task = self._task(task.id)
        # on libère les autres blocs ouverts de la tâche (ils seront replanifiés)
        free = [(blk.start, blk.end)]
        for other in self.db.list_blocks(task.id, open_only=True):
            free.append((other.start, other.end))
            if other.calendar_event_id:
                self.calendar.delete_event(other.calendar_event_id)
            self.db.delete_block(other.id)
        if count >= self.rules.max_reschedules:
            if outcome in ("rescheduled", "missed") and blk.calendar_event_id:
                self.calendar.delete_event(blk.calendar_event_id)
            self.db.set_task_status(task.id, "pending")
            self.notify(
                f"🔁 <b>{fmt.esc(task.title)}</b> a été reportée {count} fois.\nOn redécoupe ou on abandonne ?",
                [("Redécouper", f"split:{task.id}"), ("Abandonner", f"drop:{task.id}")],
            )
            return "🔁 Trop de reports : je te demande quoi faire."
        done_min = sum(b.minutes for b in self.db.list_blocks(task.id) if b.outcome == "done")
        remaining = max(task.estimated_minutes - done_min, self.rules.min_block_minutes)
        # rien n'a été fait sur un bloc reporté ou manqué : son événement est déplacé plutôt que gardé
        reuse = ({task.id: blk.calendar_event_id}
                 if outcome in ("rescheduled", "missed") and blk.calendar_event_id else None)
        not_before = self._not_before(when) if outcome == "rescheduled" else None
        res = self._place([task.model_copy(update={"estimated_minutes": remaining})], reuse, not_before, free)
        nxt = next(iter(res.blocks), None)
        return (f"🔄 Replacé : <b>{fmt.esc(task.title)}</b>\n{fmt.when(nxt.start)}" if nxt
                else "⚠️ Aucun créneau trouvé.")

    def postpone_task(self, task_id: str, when: str = "tomorrow") -> str:
        """Report d'une tâche entière (depuis /tasks) : ses blocs ouverts sont replacés demain ou lundi prochain."""
        task = self._task(task_id)
        if task.status in ("done", "dropped"):
            return f"❓ <b>{fmt.esc(task.title)}</b> n'est plus ouverte."
        nxt = self._replan_task(task, self._not_before(when))
        if nxt is None:
            self.db.set_task_status(task.id, "pending")
            return f"⚠️ Aucun créneau trouvé pour <b>{fmt.esc(task.title)}</b>."
        return f"⏭ Reporté : <b>{fmt.esc(task.title)}</b>\n{fmt.when(nxt.start)}"

    def _replan_task(self, task: Task, not_before: datetime | None) -> Block | None:
        """Libère les blocs ouverts de la tâche (un événement est déplacé, les autres supprimés) et la replace au
        plus tôt à partir de `not_before` (ou de maintenant). Retourne son premier nouveau bloc, ou None."""
        reuse: dict[str, str] = {}
        free = []
        for b in self.db.list_blocks(task.id, open_only=True):
            free.append((b.start, b.end))
            if b.calendar_event_id and task.id not in reuse:
                reuse[task.id] = b.calendar_event_id
            elif b.calendar_event_id:
                self.calendar.delete_event(b.calendar_event_id)
            self.db.delete_block(b.id)
        done_min = sum(b.minutes for b in self.db.list_blocks(task.id) if b.outcome == "done")
        remaining = max(task.estimated_minutes - done_min, self.rules.min_block_minutes)
        res = self._place([task.model_copy(update={"estimated_minutes": remaining})], reuse, not_before, free)
        return next(iter(res.blocks), None)

    def start_now(self, task_id: str) -> str:
        """Bouton « Je m'y mets » : replace la tâche au plus tôt, à partir de maintenant."""
        task = self._task(task_id)
        if task.status in ("done", "dropped"):
            return f"❓ <b>{fmt.esc(task.title)}</b> n'est plus ouverte."
        nxt = self._replan_task(task, None)
        if nxt is None:
            self.db.set_task_status(task.id, "pending")
            return f"⚠️ Aucun créneau trouvé pour <b>{fmt.esc(task.title)}</b>."
        return f"▶️ Placé : <b>{fmt.esc(task.title)}</b>\n{fmt.when(nxt.start)}"

    def split_task(self, task_id: str) -> str:
        task = self._task(task_id)
        for b in self.db.list_blocks(task.id, open_only=True):
            if b.calendar_event_id:
                self.calendar.delete_event(b.calendar_event_id)
            self.db.delete_block(b.id)
        self.db.set_task_status(task.id, "dropped")
        half = max(15, task.estimated_minutes // 2)
        parts = [
            Task(id=new_id(), title=f"{task.title} ({i}/2)", due=task.due, estimated_minutes=half,
                 priority=task.priority, source_id=task.source_id,
                 project_id=task.project_id, milestone_id=task.milestone_id)
            for i in (1, 2)
        ]
        self.schedule_tasks(parts)
        return f"✂️ <b>{fmt.esc(task.title)}</b> redécoupée en 2 tâches de {fmt.dur(half)}."

    def drop_task(self, task_id: str) -> str:
        task = self._task(task_id)
        for b in self.db.list_blocks(task.id, open_only=True):
            if b.calendar_event_id:
                self.calendar.delete_event(b.calendar_event_id)
            self.db.delete_block(b.id)
        self.db.set_task_status(task.id, "dropped")
        return f"🗑 <b>{fmt.esc(task.title)}</b> abandonnée."

    def _block(self, block_id: str) -> Block:
        b = self.db.get_block(block_id)
        if b is None:
            raise KeyError(f"bloc inconnu : {block_id}")
        return b

    def _task(self, task_id: str) -> Task:
        t = self.db.get_task(task_id)
        if t is None:
            raise KeyError(f"tâche inconnue : {task_id}")
        return t


def _locked(fn: Callable) -> Callable:
    @functools.wraps(fn)
    def wrapper(self: CoachScheduler, *args, **kwargs):
        with self.lock:
            return fn(self, *args, **kwargs)
    return wrapper


for _name in ("schedule_tasks", "_place", "replan_all", "tick", "resolve_conflicts", "handle_callback",
              "mark_done", "report", "postpone_task", "split_task", "drop_task",
              "adjust_importance", "sync_styles", "start_now"):
    setattr(CoachScheduler, _name, _locked(getattr(CoachScheduler, _name)))
