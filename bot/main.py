"""Point d'entrée : bot Telegram en long polling, restreint à TELEGRAM_ALLOWED_USER_ID."""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Mapping

from .handlers import COMMANDS, BotService, TelegramHandlers
from .ports import CoachAdapter, MemoryAdapter, RouterAdapter

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Config:
    token: str
    allowed_user_id: int


def load_config(env: Mapping[str, str] | None = None) -> Config:
    """Lit TELEGRAM_BOT_TOKEN et TELEGRAM_ALLOWED_USER_ID (env, après chargement de .env)."""
    if env is None:
        try:
            from dotenv import load_dotenv
            load_dotenv()
        except ImportError:
            pass
        env = os.environ
    token = env.get("TELEGRAM_BOT_TOKEN", "").strip()
    uid = env.get("TELEGRAM_ALLOWED_USER_ID", "").strip()
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN manquant dans .env")
    if not uid.isdigit():
        raise RuntimeError("TELEGRAM_ALLOWED_USER_ID manquant ou invalide dans .env")
    return Config(token=token, allowed_user_id=int(uid))


def build_application(config: Config, service: BotService | None = None) -> Any:
    from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                              MessageHandler, filters)

    if service is None:
        import journal  # tout ce que tu écris ou dis est gardé dans le Journal Google Docs
        service = BotService(CoachAdapter(), MemoryAdapter(), RouterAdapter(),
                             config.allowed_user_id, journal=journal.add)
    h = TelegramHandlers(service)
    only_me = filters.User(user_id=config.allowed_user_id)  # filtre strict, doublé dans les handlers
    # concurrent_updates : un traitement lent (LLM, Google) ne bloque plus les clics sur les boutons
    async def publish_menu(application: Any) -> None:  # le menu « / » de Telegram liste les commandes
        from telegram import BotCommand
        try:
            await application.bot.set_my_commands([BotCommand(n, d[:256]) for n, d in COMMANDS])
        except Exception:  # noqa: BLE001 - un menu manquant ne doit pas empêcher le bot de démarrer
            log.warning("menu des commandes non publié", exc_info=True)

    app = (Application.builder().token(config.token).concurrent_updates(True)
           .post_init(publish_menu).build())
    app.add_handler(CommandHandler("ask", h.ask, filters=only_me))
    app.add_handler(CommandHandler("today", h.today, filters=only_me))
    app.add_handler(CommandHandler("tasks", h.tasks, filters=only_me))
    app.add_handler(CommandHandler("replan", h.replan, filters=only_me))
    app.add_handler(CommandHandler("focus", h.focus, filters=only_me))
    app.add_handler(CommandHandler("projet", h.projet, filters=only_me))
    app.add_handler(CommandHandler("projets", h.projets, filters=only_me))
    app.add_handler(CommandHandler("annuler", h.annuler, filters=only_me))
    app.add_handler(CommandHandler("semaine", h.week, filters=only_me))
    app.add_handler(CommandHandler("quota", h.quota, filters=only_me))
    app.add_handler(CommandHandler("note", h.note, filters=only_me))
    app.add_handler(CommandHandler("done", h.done, filters=only_me))
    app.add_handler(CommandHandler(["help", "start"], h.help, filters=only_me))
    app.add_handler(MessageHandler(only_me & filters.VOICE, h.voice))
    app.add_handler(MessageHandler(only_me & (filters.PHOTO | filters.Document.ALL), h.document))
    app.add_handler(MessageHandler(only_me & filters.TEXT & ~filters.COMMAND, h.text))
    app.add_handler(CallbackQueryHandler(h.callback))  # garde d'identité dans h.callback

    async def on_error(update: Any, context: Any) -> None:
        log.error("erreur handler", exc_info=context.error)
        msg = getattr(update, "effective_message", None)
        if msg is not None:  # jamais de silence : on dit ce qui a échoué
            import html
            err = context.error
            try:  # les pannes répétées déclenchent une alerte (avec délai anti-spam)
                import health
                health.report_exception("bot", err)
            except Exception:  # noqa: BLE001
                pass
            hint = ("\nLe contenu est privé et Ollama n'est pas installé." if "Ollama" in repr(err)
                    or "PrivateModelUnavailable" in type(err).__name__ else "")
            await msg.reply_text(f"⚠️ <b>Oups</b>, ça n'a pas marché.\n<code>{html.escape(type(err).__name__)}</code>"
                                 f"{hint}", parse_mode="HTML")

    app.add_error_handler(on_error)
    return app
