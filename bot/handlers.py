"""Logique du bot (BotService, sans dépendance Telegram) + couche Telegram (TelegramHandlers)."""
from __future__ import annotations

import asyncio
import logging
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from coach import fmt

from . import dates
from .ports import CoachPort, MemoryPort, NotReadyError, RouterPort, Sensitivity
from .transcribe import transcribe_audio

log = logging.getLogger(__name__)

Buttons = list[list[tuple[str, str]]]  # lignes de (libellé, callback_data)

DURATIONS = (10, 15, 30, 45, 60, 90, 120, 180)
MAX_TASK_CARDS = 20  # /tasks : au-delà, Telegram risque de limiter l'envoi
PUBLIC_HINTS = ("cours", "td", "tp", "recette", "public", "poly", "slides", "support")
PRIVATE_HINTS = ("perso", "privé", "prive", "private", "confidentiel", "mail", "facture", "médical")


@dataclass
class Reply:
    text: str
    buttons: Buttons = field(default_factory=list)


# (commande, description courte) : source unique du /help et du menu « / » de Telegram
COMMANDS: list[tuple[str, str]] = [
    ("ask", "Poser une question à ta mémoire (documents, notes, agenda)"),
    ("today", "Agenda et tâches du jour"),
    ("replan", "Refaire tout le planning (urgent d'abord)"),
    ("focus", "Tes 3 priorités du moment, avec la raison de chacune"),
    ("projet", "Créer un projet : je t'interroge pour le définir"),
    ("projets", "Avancement de tes projets"),
    ("annuler", "Abandonner l'entretien de projet en cours"),
    ("tasks", "Tâches ouvertes, avec boutons fait / reporter / semaine prochaine"),
    ("semaine", "Récap de la semaine"),
    ("note", "Garder une info telle quelle (ni tâche ni rendez-vous)"),
    ("done", "Marquer une tâche terminée : /done identifiant"),
    ("quota", "Consommation des API IA gratuites du jour"),
    ("help", "Afficher cette liste"),
]


def task_buttons(task_id: str) -> Buttons:
    return [[("Planifier", f"plan:{task_id}"), ("Modifier la durée", f"dur:{task_id}"),
             ("Ignorer", f"ign:{task_id}")]]


def event_buttons(event_id: str) -> Buttons:
    return [[("Ajouter à l'agenda", f"evadd:{event_id}"), ("Ignorer", f"evign:{event_id}")]]


def reminder_buttons(block_id: str) -> Buttons:
    return [[("Oui", f"ok:{block_id}"), ("Pas fini", f"nf:{block_id}"), ("Reporter", f"rep:{block_id}"),
             ("Semaine pro.", f"nw:{block_id}")]]


def open_task_buttons(task_id: str) -> Buttons:
    """Boutons sous chaque tâche de /tasks : fait / reporté à demain / à la semaine prochaine / ignoré."""
    return [[("✅ Fait", f"td:{task_id}"), ("⏭ Demain", f"tpo:{task_id}"),
             ("📅 Semaine pro.", f"tnw:{task_id}"), ("🗑", f"ign:{task_id}")]]


def guess_sensitivity(filename: str, caption: str | None) -> Sensitivity | None:
    """None si ce n'est pas évident (=> on demande)."""
    blob = f"{filename} {caption or ''}".lower()
    priv = any(h in blob for h in PRIVATE_HINTS)
    pub = any(h in blob for h in PUBLIC_HINTS)
    if priv and not pub:
        return "private"
    if pub and not priv:
        return "public"
    return None


QUESTION_RE = re.compile(
    r"^\s*(qu['’ ]?est[- ]ce|qu['’]est|quoi|quel(le)?s?\b|quand\b|combien\b|comment\b|o[uù]\b|est[- ]ce\b|"
    r"ai[- ]je\b|j['’]ai (quoi|quelque|des|un|une|du|rdv|cours)|c['’]est quoi|"
    r"(mon|ma|mes) (programme|planning|agenda|emploi du temps|journ[ée]e|semaine|prochain)|"
    r"(il y a|y a[- ]t[- ]il) )", re.I)


POSTPONE_RE = re.compile(r"^\s*report(?:e|er|ez)\s+(.+?)\s+(?:au|à|a|en|pour le|pour|jusqu'au|jusqu'à)\s+(.+?)\s*$", re.I)
INFO_RE = re.compile(r"^\s*(note|info|retiens|retenir|rappelle[- ]toi|m[ée]mo)\s*[:\-–]\s*", re.I)


def is_question(text: str) -> bool:
    """Question sur ses données (agenda, notes...) plutôt qu'une note ou une tâche à enregistrer."""
    t = text.strip()
    return len(t) > 3 and (t.endswith("?") or bool(QUESTION_RE.match(t)))


def _note_sensitivity() -> Sensitivity:
    """`public` (donc cloud autorisé) seulement si ALLOW_CLOUD_FOR_NOTES=true dans .env."""
    try:
        from llm_router.router import cloud_allowed_for_notes
        return "public" if cloud_allowed_for_notes() else "private"
    except ImportError:
        return "private"


class BotService:
    """Logique pure : renvoie des Reply, ne touche pas à Telegram."""

    def __init__(self, coach: CoachPort, memory: MemoryPort, router: RouterPort,
                 allowed_user_id: int, journal: Callable[[str, str], None] | None = None) -> None:
        self.coach, self.memory, self.router = coach, memory, router
        self.allowed_user_id = allowed_user_id
        self.journal = journal  # (texte, source) -> Journal Google Docs ; jamais bloquant

    def is_allowed(self, user_id: int | None) -> bool:
        return user_id is not None and user_id == self.allowed_user_id

    def _log(self, text: str, source: str) -> str:
        """Ajoute au journal Google Docs. Retourne un court suffixe pour la confirmation."""
        if self.journal is None:
            return ""
        try:
            self.journal(text, source)
            return " · 📓 journal"
        except Exception:  # noqa: BLE001 - le journal ne doit jamais empêcher d'enregistrer la note
            log.warning("journal indisponible", exc_info=True)
            return ""

    # --- entrées ---
    def note(self, text: str, source: str = "note") -> list[Reply]:
        """Garde une information telle quelle : ni tâche, ni rendez-vous, ni extraction."""
        text = text.strip()
        if not text:
            return [Reply("Usage : /note &lt;information à retenir&gt;")]
        sens = _note_sensitivity()
        try:
            self.memory.add_note(text, sens, "telegram")
        except NotReadyError as exc:
            return [Reply(f"⚠️ Module indisponible pour le moment ({fmt.esc(exc)}).")]
        return [Reply(f"📌 <b>Noté</b>{self._log(text, source)}\n<i>{fmt.esc(text[:300])}</i>")]

    def replan(self) -> list[Reply]:
        try:
            return [Reply(self.coach.replan_all())]
        except NotReadyError as exc:
            return [Reply(f"⚠️ Coach indisponible ({fmt.esc(exc)}).")]

    def focus(self) -> list[Reply]:
        try:
            text, buttons = self.coach.focus()
        except NotReadyError as exc:
            return [Reply(f"⚠️ Coach indisponible ({fmt.esc(exc)}).")]
        return [Reply(text, buttons)]

    def postpone_to(self, title_part: str, when_text: str, today: Any = None) -> list[Reply] | None:
        """« reporte IA au 4 janvier ». None si ce n'est pas un report compris (date illisible)."""
        from datetime import date as _date
        day = dates.parse_date(when_text, today or _date.today())
        if day is None:
            return None
        try:
            tasks = self.coach.list_open_tasks()
            key = dates.fold(title_part)
            hits = [t for t in tasks if key and key in dates.fold(t.title)]
            if not hits:
                return [Reply(f"❓ Aucune tâche ouverte ne contient « {fmt.esc(title_part)} ».")]
            if len(hits) > 1:
                names = "\n".join(f"• {fmt.esc(t.title)}" for t in hits[:8])
                return [Reply(f"❓ Plusieurs tâches correspondent, précise :\n{names}")]
            return [Reply(self.coach.postpone_task(str(hits[0].id), day.isoformat()))]
        except NotReadyError as exc:
            return [Reply(f"⚠️ Coach indisponible ({fmt.esc(exc)}).")]

    # --- projets ---
    @staticmethod
    def _replies(msgs: list[tuple[str, list]]) -> list[Reply]:
        return [Reply(text, buttons) for text, buttons in msgs]

    def projet(self) -> list[Reply]:
        try:
            return self._replies(self.coach.interview_start())
        except NotReadyError as exc:
            return [Reply(f"⚠️ Coach indisponible ({fmt.esc(exc)}).")]

    def projets(self) -> list[Reply]:
        try:
            text, buttons = self.coach.projects_overview()
        except NotReadyError as exc:
            return [Reply(f"⚠️ Coach indisponible ({fmt.esc(exc)}).")]
        return [Reply(text, buttons)]

    def annuler(self) -> list[Reply]:
        try:
            return self._replies(self.coach.interview_cancel())
        except NotReadyError as exc:
            return [Reply(f"⚠️ Coach indisponible ({fmt.esc(exc)}).")]

    def _task_card(self, t: Any, header: str | None = None) -> Reply:
        """Carte d'une tâche détectée, avec la proposition de projet s'il y en a une."""
        text = fmt.task_card(t) if header is None else fmt.task_card(t, header)
        buttons = task_buttons(str(t.id))
        try:
            prop = self.coach.project_proposal(str(t.id))
        except Exception:  # noqa: BLE001 - une proposition manquante ne doit jamais empêcher la carte
            prop = None
        if prop:
            text, buttons = f"{text}\n{prop[0]}", buttons + prop[1]
        return Reply(text, buttons)

    def on_text(self, text: str, source: str = "texte") -> list[Reply]:
        try:
            active = self.coach.interview_active()
        except Exception:  # noqa: BLE001 - un entretien illisible ne doit jamais bloquer les textes ordinaires
            log.warning("état de l'entretien illisible", exc_info=True)
            active = False
        if active:  # entretien de projet ouvert : ce message lui est destiné
            try:
                msgs = self.coach.interview_text(text)
            except NotReadyError:
                msgs = []
            except Exception:  # noqa: BLE001 - une panne de l'entretien ne doit jamais faire perdre le message
                log.warning("entretien de projet en échec, message traité normalement", exc_info=True)
                msgs = []
            if msgs:  # vide (brouillon expiré entre-temps) : on retombe sur le flux normal, rien n'est perdu
                return self._replies(msgs)
        pm = POSTPONE_RE.match(text)
        if pm:  # « reporte IA au 4 janvier » : report à une date libre
            done = self.postpone_to(pm[1], pm[2])
            if done is not None:
                return done
        if is_question(text):  # « j'ai quoi cet aprem ? » : on répond, on ne l'enregistre pas comme note
            return self.ask(text)
        m = INFO_RE.match(text)
        if m:  # « note : le code du garage est 4721 » -> information gardée, rien d'autre
            return self.note(text[m.end():], "note")
        sens = _note_sensitivity()
        label = "publique" if sens == "public" else "privée"
        try:
            note_id = self.memory.add_note(text, sens, "telegram")
            replies = [Reply(f"📝 Note enregistrée ({label}){self._log(text, source)}.")]
            tasks, events = self.coach.extract_items(text, f"note:{note_id}", sens)
            # rendez-vous donnés directement par l'utilisateur : ajoutés tout de suite à l'agenda
            for ev in events:
                replies.append(Reply(self.coach.add_event(str(ev.id))))
        except NotReadyError as exc:
            return [Reply(f"⚠️ Module indisponible pour le moment ({fmt.esc(exc)}).")]
        for t in tasks:
            card = self._task_card(t)
            if getattr(t, "due", None) is None and getattr(t, "not_before", None) is None:
                try:  # sans date, rien n'attend : on la place tout de suite (le bouton Planifier reste pour replacer)
                    card.text += "\n" + self.coach.schedule(str(t.id))
                except Exception:  # noqa: BLE001 - la carte et ses boutons restent, planifiable à la main
                    log.exception("planification automatique impossible pour %s", t.id)
            replies.append(card)
        return replies

    def on_voice(self, audio_path: str) -> list[Reply]:
        try:
            text = transcribe_audio(audio_path, self.router)
        except NotReadyError as exc:
            return [Reply(f"⚠️ Transcription indisponible ({fmt.esc(exc)}).")]
        if not text:
            return [Reply("🎙 Je n'ai rien compris au vocal.")]
        return [Reply(f"🎙 <b>Transcription</b>\n<i>{fmt.esc(text)}</i>")] + self.on_text(text, source="vocal")

    def on_document(self, path: str, filename: str, caption: str | None, kind: str) -> list[Reply]:
        guess = guess_sensitivity(filename, caption)
        sens: Sensitivity = guess or "private"  # défaut sûr en attendant la réponse
        try:
            doc_id = self.memory.add_document(path, filename, sens, kind)
        except NotReadyError as exc:
            return [Reply(f"⚠️ Indexation indisponible ({fmt.esc(exc)}).")]
        describe = getattr(self.memory, "describe", None)
        if describe is not None:  # Drive actif : le dossier décide de la sensibilité
            try:
                text, buttons = describe(doc_id)
                return [Reply(fmt.esc(text), [buttons[i:i + 3] for i in range(0, len(buttons), 3)] if buttons else [])]
            except (NotReadyError, AttributeError):
                pass
        if guess is not None:
            return [Reply(f"📄 {fmt.esc(filename)} indexé ({'public' if guess == 'public' else 'privé'}).")]
        return [Reply(f"📄 {fmt.esc(filename)} indexé (privé pour l'instant). Privé ou public ?",
                      [[("Privé", f"prv:{doc_id}"), ("Public", f"pub:{doc_id}")]])]

    # --- commandes ---
    def ask(self, question: str) -> list[Reply]:
        question = question.strip()
        if not question:
            return [Reply("Usage : /ask &lt;question&gt;")]
        try:
            ctx_fn = getattr(self.coach, "agenda_context", None)
            ctx = ctx_fn() if ctx_fn else None  # date du jour + agenda + échéances
            ans = self.memory.ask(question, context=ctx)
        except NotReadyError as exc:
            return [Reply(f"⚠️ Mémoire indisponible ({fmt.esc(exc)}).")]
        src = ", ".join(fmt.esc(s) for s in ans.sources) if ans.sources else "aucune"
        return [Reply(f"💬 {fmt.md_to_html(ans.text)}\n\n📎 <b>Source</b> : {src}")]

    def today(self) -> list[Reply]:
        try:
            return [Reply(self.coach.today_agenda())]
        except NotReadyError as exc:
            return [Reply(f"⚠️ Coach indisponible ({fmt.esc(exc)}).")]

    def help(self) -> list[Reply]:
        lines = [f"/{name} — {fmt.esc(desc)}" for name, desc in COMMANDS]
        return [Reply("🧭 <b>Commandes</b>\n\n" + "\n".join(lines)
                      + "\n\n<i>Sans commande : un texte = note (tâches/rendez-vous détectés), "
                        "une question = réponse, un vocal, une photo ou un PDF = traités.</i>")]

    def quota(self) -> list[Reply]:
        import quota  # consommation des API gratuites, lue dans data/usage.log
        return [Reply(quota.report())]

    def week(self) -> list[Reply]:
        try:
            return [Reply(self.coach.weekly_recap())]
        except NotReadyError as exc:
            return [Reply(f"⚠️ Coach indisponible ({fmt.esc(exc)}).")]

    def tasks(self) -> list[Reply]:
        try:
            tasks = self.coach.list_open_tasks()
        except NotReadyError as exc:
            return [Reply(f"⚠️ Coach indisponible ({fmt.esc(exc)}).")]
        if not tasks:
            return [Reply("📋 Aucune tâche ouverte.")]
        replies = [Reply(f"📋 <b>Tâches ouvertes</b> ({len(tasks)})", self.coach.task_folders())]
        tasks = [t for t in tasks if not getattr(t, "project_id", None)]  # celles des projets : dans leur dossier
        for t in tasks[:MAX_TASK_CARDS]:  # une carte par tâche, avec ses boutons
            extra = (f" · le {fmt.day_short(t.due)}" if fmt.on_day(t)
                     else f" · avant {fmt.day_short(t.due)}" if getattr(t, "due", None) else "")
            replies.append(Reply(f"{fmt.STATUS_ICON.get(t.status, '•')} <b>{fmt.esc(t.title)}</b> · "
                                 f"{fmt.dur(t.estimated_minutes)}{extra}", open_task_buttons(str(t.id))))
        if len(tasks) > MAX_TASK_CARDS:
            replies.append(Reply(f"<i>… et {len(tasks) - MAX_TASK_CARDS} autres (/done + identifiant)</i>"))
        return replies

    def _task_title(self, task_id: str) -> str:
        return next((t.title for t in self.coach.list_open_tasks() if str(t.id) == task_id), task_id)

    def done(self, task_id: str) -> list[Reply]:
        task_id = task_id.strip()
        if not task_id:
            return [Reply("Usage : /done &lt;id&gt;")]
        try:
            ok = self.coach.mark_done(task_id)
        except NotReadyError as exc:
            return [Reply(f"⚠️ Coach indisponible ({fmt.esc(exc)}).")]
        return [Reply(f"✅ Tâche <code>{fmt.esc(task_id)}</code> marquée comme faite." if ok
                      else f"❓ Tâche <code>{fmt.esc(task_id)}</code> introuvable.")]

    # --- boutons ---
    def on_callback(self, data: str) -> Reply:
        action, _, rest = data.partition(":")
        try:
            if action == "plan":
                return Reply(self.coach.schedule(rest))
            if action == "ign":
                self.coach.ignore(rest)
                return Reply("🗑 Tâche ignorée.")
            if action == "dur":
                return Reply("⏱ Quelle durée ?",
                             [[(fmt.dur(m), f"durset:{rest}:{m}") for m in DURATIONS[i:i + 4]]
                              for i in range(0, len(DURATIONS), 4)])
            if action == "durset":
                task_id, _, minutes = rest.rpartition(":")
                moved = self.coach.set_duration(task_id, int(minutes))
                return Reply(f"⏱ Durée fixée à {fmt.dur(int(minutes))}." + (f"\n{moved}" if moved else ""),
                             task_buttons(task_id)[:1])
            if action in ("ok", "nf", "rep", "nw"):
                outcome = {"ok": "done", "nf": "not_finished", "rep": "rescheduled", "nw": "next_week"}[action]
                return Reply(self.coach.report_outcome(rest, outcome))
            if action == "td":
                title = self._task_title(rest)
                return Reply(f"✅ <b>{fmt.esc(title)}</b> est faite." if self.coach.mark_done(rest)
                             else "❓ Tâche introuvable.")
            if action == "nudgego":  # relance : « Je m'y mets »
                return Reply(self.coach.start_now(rest))
            if action == "nudgeno":
                return Reply("👌 D'accord, je ne te relance plus là-dessus aujourd'hui.")
            if action in ("fup", "fdn"):  # correction d'importance : on renvoie le focus recalculé
                text, buttons = self.coach.adjust_importance(rest, action == "fup")
                return Reply(text, buttons)
            if action == "pi":  # entretien de projet : un ou plusieurs messages, affichés ensemble
                msgs = self.coach.interview_callback(rest)
                return Reply("\n\n".join(t for t, _ in msgs), msgs[-1][1] if msgs else [])
            if action in ("pjv", "pm", "pp"):
                text, buttons = self.coach.project_action(action, rest)
                return Reply(text, buttons)
            if action in ("ps", "pa"):  # suggestions de tâches et rangement dans les étapes
                text, buttons = self.coach.assistant_action(action, rest)
                return Reply(text, buttons)
            if action in ("pjok", "pjwho", "pjno", "pjset", "pjms"):
                task_id, _, arg = rest.partition(":")
                text, buttons = self.coach.project_pick(action, task_id, arg or None)
                return Reply(text, buttons)
            if action == "ptk":  # dossier d'un projet depuis /tasks
                text, buttons = self.coach.project_tasks_view(rest)
                return Reply(text, buttons)
            if action == "ptd":  # tâche faite depuis un dossier : on réaffiche le dossier
                project_id, _, task_id = rest.partition(":")
                self.coach.mark_done(task_id)
                text, buttons = self.coach.project_tasks_view(project_id)
                return Reply(text, buttons)
            if action == "ptl":  # retour à /tasks : les dossiers
                return Reply("📋 <b>Tâches ouvertes</b>", self.coach.task_folders())
            if action in ("tpo", "tnw"):
                return Reply(self.coach.postpone_task(rest, "tomorrow" if action == "tpo" else "next_week"))
            if action == "mv":
                doc_id, _, key = rest.partition(":")
                return Reply(fmt.esc(self.memory.move(doc_id, key)))
            if action == "evadd":
                return Reply(self.coach.add_event(rest))
            if action == "evign":
                return Reply(self.coach.ignore_event(rest))
            if action in ("split", "drop"):
                return Reply(self.coach.resolve(action, rest))
            if action in ("pub", "prv"):
                sens: Sensitivity = "public" if action == "pub" else "private"
                self.memory.set_sensitivity(rest, sens)
                return Reply(f"📄 Document marqué {'public' if sens == 'public' else 'privé'}.")
        except NotReadyError as exc:
            return Reply(f"⚠️ Module indisponible ({fmt.esc(exc)}).")
        except (ValueError, KeyError) as exc:
            log.warning("callback invalide %r: %s", data, exc)
        return Reply("❓ Action inconnue.")


# ---------------------------------------------------------------------------
# Couche Telegram (duck-typed : testable sans réseau)
# ---------------------------------------------------------------------------

def to_markup(buttons: Buttons) -> Any:
    """InlineKeyboardMarkup si python-telegram-bot est installé, sinon la liste brute."""
    if not buttons:
        return None
    try:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    except ImportError:
        return buttons
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(label, callback_data=cb) for label, cb in row] for row in buttons])


def _with_status(text: str) -> str:
    """Préfixe ✅ si la réponse n'a pas déjà son propre pictogramme (⚠️, ❓…)."""
    return text if text[:1] and not text[:1].isalnum() else f"✅ {text}"


TEXT_WAIT = 4.0  # secondes d'attente, pendant un entretien de projet, pour regrouper les messages d'affilée


class TelegramHandlers:
    def __init__(self, service: BotService, text_wait: float = TEXT_WAIT) -> None:
        self.service = service
        self.text_wait = text_wait
        self._buf: list[str] = []  # messages reçus pendant un entretien, traités ensemble
        self._seq = 0

    def _allowed(self, update: Any) -> bool:
        user = getattr(update, "effective_user", None)
        return self.service.is_allowed(getattr(user, "id", None))

    @staticmethod
    async def _send(update: Any, replies: list[Reply]) -> None:
        for r in replies:
            await update.message.reply_text(r.text, reply_markup=to_markup(r.buttons), parse_mode="HTML")

    @staticmethod
    async def _download(media: Any, suffix: str) -> str:
        tg_file = await media.get_file()
        fd, path = tempfile.mkstemp(suffix=suffix, prefix="bot_")
        import os
        os.close(fd)
        await tg_file.download_to_drive(path)
        return path

    async def text(self, update: Any, context: Any) -> None:
        if not self._allowed(update) or not update.message or not update.message.text:
            return
        text = update.message.text
        if self.text_wait > 0 and (self._buf or await self._interview_open()):
            # entretien de projet : une description envoyée en plusieurs morceaux = une seule réponse
            self._buf.append(text)
            self._seq += 1
            mine = self._seq
            await asyncio.sleep(self.text_wait)
            if self._seq != mine:  # un message plus récent a pris le relais
                return
            text, self._buf = "\n".join(self._buf), []
        await self._send(update, await asyncio.to_thread(self.service.on_text, text))

    async def _interview_open(self) -> bool:
        try:
            return await asyncio.to_thread(self.service.coach.interview_active)
        except Exception:  # noqa: BLE001 - un état illisible ne doit jamais retarder ni perdre un message
            return False

    async def voice(self, update: Any, context: Any) -> None:
        if not self._allowed(update) or not update.message:
            return
        media = update.message.voice
        if media is None:
            return
        path = await self._download(media, ".ogg")
        try:
            await self._send(update, await asyncio.to_thread(self.service.on_voice, path))
        finally:
            Path(path).unlink(missing_ok=True)

    async def document(self, update: Any, context: Any) -> None:
        """PDF ou photo. La mémoire envoie le fichier sur Drive (ou en lit le texte) : la copie locale est supprimée."""
        if not self._allowed(update) or not update.message:
            return
        msg = update.message
        caption = getattr(msg, "caption", None)
        if getattr(msg, "photo", None):
            media, kind, name, suffix = msg.photo[-1], "photo", "photo.jpg", ".jpg"
        elif getattr(msg, "document", None):
            media = msg.document
            name = getattr(media, "file_name", None) or "document"
            if getattr(media, "mime_type", None) == "application/pdf" or name.lower().endswith(".pdf"):
                kind, suffix = "pdf", ".pdf"
            else:
                await msg.reply_text("Format non pris en charge (PDF ou photo uniquement).")
                return
        else:
            return
        path = await self._download(media, suffix)
        try:
            await self._send(update, await asyncio.to_thread(self.service.on_document, path, name, caption, kind))
        finally:
            Path(path).unlink(missing_ok=True)

    async def ask(self, update: Any, context: Any) -> None:
        if not self._allowed(update) or not update.message:
            return
        await self._send(update, await asyncio.to_thread(self.service.ask, " ".join(context.args or [])))

    async def today(self, update: Any, context: Any) -> None:
        if not self._allowed(update) or not update.message:
            return
        await self._send(update, await asyncio.to_thread(self.service.today))

    async def note(self, update: Any, context: Any) -> None:
        if not self._allowed(update) or not update.message:
            return
        await self._send(update, await asyncio.to_thread(self.service.note, " ".join(context.args or [])))

    async def help(self, update: Any, context: Any) -> None:
        if not self._allowed(update) or not update.message:
            return
        await self._send(update, self.service.help())

    async def quota(self, update: Any, context: Any) -> None:
        if not self._allowed(update) or not update.message:
            return
        await self._send(update, await asyncio.to_thread(self.service.quota))

    async def week(self, update: Any, context: Any) -> None:
        if not self._allowed(update) or not update.message:
            return
        await self._send(update, await asyncio.to_thread(self.service.week))

    async def replan(self, update: Any, context: Any) -> None:
        if not self._allowed(update) or not update.message:
            return
        await self._send(update, await asyncio.to_thread(self.service.replan))

    async def focus(self, update: Any, context: Any) -> None:
        if not self._allowed(update) or not update.message:
            return
        await self._send(update, await asyncio.to_thread(self.service.focus))

    async def projet(self, update: Any, context: Any) -> None:
        if not self._allowed(update) or not update.message:
            return
        await self._send(update, await asyncio.to_thread(self.service.projet))

    async def projets(self, update: Any, context: Any) -> None:
        if not self._allowed(update) or not update.message:
            return
        await self._send(update, await asyncio.to_thread(self.service.projets))

    async def annuler(self, update: Any, context: Any) -> None:
        if not self._allowed(update) or not update.message:
            return
        await self._send(update, await asyncio.to_thread(self.service.annuler))

    async def tasks(self, update: Any, context: Any) -> None:
        if not self._allowed(update) or not update.message:
            return
        await self._send(update, await asyncio.to_thread(self.service.tasks))

    async def done(self, update: Any, context: Any) -> None:
        if not self._allowed(update) or not update.message:
            return
        args = context.args or []
        await self._send(update, await asyncio.to_thread(self.service.done, args[0] if args else ""))

    async def callback(self, update: Any, context: Any) -> None:
        if not self._allowed(update):
            return
        q = update.callback_query
        if q is None:
            return
        if q.data == "noop":  # bouton « En cours… » : rien à faire
            await q.answer("⏳ Patiente un instant…")
            return
        await q.answer("⏳")  # accusé immédiat : le bouton réagit tout de suite, le traitement suit
        # le clavier devient « En cours… » : clic visiblement pris en compte, et pas de double-clic
        pending = getattr(q, "edit_message_reply_markup", None)
        if pending is not None:
            try:
                await pending(reply_markup=to_markup([[("⏳ En cours…", "noop")]]))
            except Exception:
                log.debug("indicateur « en cours » non affiché", exc_info=True)
        try:
            reply = await asyncio.to_thread(self.service.on_callback, q.data or "")
        except Exception:
            log.exception("callback %r échoué", q.data)
            reply = Reply("⚠️ Ça n'a pas marché, rien n'a été enregistré. Réessaie.")
        await q.edit_message_text(_with_status(reply.text), reply_markup=to_markup(reply.buttons),
                                  parse_mode="HTML")
