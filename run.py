"""Lance tout : bot Telegram + coach (relances, briefing) + Gmail + Drive + Notion + surveillance.

    python run.py            # tout
    python run.py --no-gmail # sans Gmail
"""
from __future__ import annotations

import argparse
import asyncio
import functools
import json
import logging
import os
import urllib.request

from dotenv import load_dotenv

load_dotenv()

import health  # noqa: E402
import journal  # noqa: E402
import quota  # noqa: E402
from bot.main import build_application, load_config  # noqa: E402
from coach import fmt, focus, store  # noqa: E402
from coach.calendar_client import GoogleCalendarClient  # noqa: E402
from coach.db import Database  # noqa: E402
from coach.rules import load_rules  # noqa: E402
from coach.scheduler import CoachScheduler  # noqa: E402

log = logging.getLogger("run")

# le scheduler parle `done:/notfinished:/postpone:`, le bot `ok:/nf:/rep:/nw:`
_CB = {"done": "ok", "notfinished": "nf", "postpone": "rep", "nextweek": "nw"}


def _telegram_notify(token: str, chat_id: int):
    def notify(text: str, buttons=None) -> bool:
        """Envoie le message ; False si Telegram ne l'a pas reçu (l'erreur est journalisée, jamais levée)."""
        body: dict = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
        if buttons:
            rows = buttons if isinstance(buttons[0], list) else [buttons]
            body["reply_markup"] = {"inline_keyboard": [
                [{"text": label, "callback_data": _tr(cb)} for label, cb in row] for row in rows]}
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=15).read()
            return True
        except Exception:
            log.exception("envoi Telegram échoué")
            return False
    return notify


def _tr(cb: str) -> str:
    action, _, rest = cb.partition(":")
    return f"{_CB.get(action, action)}:{rest}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-gmail", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO)
    for noisy in ("httpx", "httpcore"):  # leurs URLs contiennent le token Telegram
        logging.getLogger(noisy).setLevel(logging.WARNING)

    cfg = load_config()
    notify = _telegram_notify(cfg.token, cfg.allowed_user_id)
    hl = health.Health(notify)
    health.set_instance(hl)
    health.startup_check(notify)

    db = Database()
    sched = CoachScheduler(db, GoogleCalendarClient.from_env(), load_rules(), notify)
    store.configure(db, sched)
    from llm_router import router  # l'IA ne sert qu'à reformuler la raison du focus (sinon raison calculée)
    # délai court côté HTTP aussi : un fournisseur muet ne garde pas son fil de reformulation pendant 10 min
    sched.explain_fn = functools.partial(router.complete, timeout=focus.EXPLAIN_TIMEOUT)
    sched.explain_sensitivity = "public" if router.cloud_allowed_for_notes() else "private"
    import notion_sync  # table des matières Notion : « Stage 3IF » -> préfixe `Stg`
    store.configure_projects(router.complete, sched.explain_sensitivity, notion_sync.SHORT)

    from apscheduler.schedulers.background import BackgroundScheduler
    bg = BackgroundScheduler(timezone=sched.tz)

    def add(component: str, fn, minutes: float) -> None:
        bg.add_job(health.guard(hl, component, fn), "interval", minutes=minutes,
                   next_run_time=sched.now(), max_instances=1, coalesce=True, id=component)

    add("coach", sched.tick, 5)                                  # relances, briefing, récap du dimanche
    add("heartbeat", lambda: health.beat(), 1)                    # détecte les coupures au prochain démarrage
    add("quota", lambda: quota.check_thresholds(notify, db.claim_notification), 10)
    add("journal", journal.flush_pending, 10)                     # renvoie les notes en attente vers Google Docs
    add("projects", lambda: store.projects_idle_check(notify), 15)  # propose la suite des projets sans tâche ouverte
    add("pwatch", lambda: store.projects_watch(notify), 30)         # prévient d'un projet qui stagne ou dont le rendu approche

    if not args.no_gmail:
        import ingest_gmail
        gmail = ingest_gmail.build_service()
        add("gmail", lambda: log.info("gmail : %d nouveau(x) mail(s)", ingest_gmail.poll(gmail, notify)), 15)

    if os.getenv("DRIVE_ENABLED", "true").strip().lower() == "true":
        import drive_sync

        def drive_job() -> None:
            for r in drive_sync.sync(drive_sync.get_drive()):
                where = drive_sync.FOLDERS[r.folder_key][0] if r.folder_key else "Inbox"
                notify(f"📁 <b>Drive</b> : {fmt.esc(r.filename)} rangé dans {where}.", None)

        add("drive", drive_job, 15)

    from bot.handlers import task_buttons
    notion = notion_sync.client_from_env()
    if notion is None:
        log.info("Notion non configuré (NOTION_TOKEN / NOTION_DB_ID) : synchronisation désactivée")
    else:
        sched.on_task_done = lambda tid: notion_sync.push_done(db, notion, tid)  # coché aussi dans Notion, tout de suite
        add("notion", lambda: log.info("notion : %s", notion_sync.sync(
            db, notion, notify, lambda tid: store.mark_done(tid), task_buttons, sched)), 15)

    bg.start()
    asyncio.set_event_loop(asyncio.new_event_loop())  # Python 3.14 : plus de boucle implicite
    build_application(cfg).run_polling()
    health.clean_exit()  # arrêt normal (systemctl stop/restart) : pas d'alerte au prochain démarrage


if __name__ == "__main__":
    main()
