"""Boîte mail INSA (Zimbra) en IMAP, lecture seule : mêmes cartes et même confidentialité que Gmail.

La boîte est ouverte en lecture seule (EXAMINE) et les messages lus avec BODY.PEEK : rien n'est marqué comme lu,
déplacé ou supprimé. Identifiants dans .env uniquement (INSA_IMAP_HOST, INSA_IMAP_USER, INSA_IMAP_PASSWORD).
"""
from __future__ import annotations

import email
import html
import imaplib
import logging
import os
import re
from datetime import date, datetime, timedelta
from email import policy
from typing import Any, Callable

import memory
from ingest_gmail import Notify, process_mail

log = logging.getLogger("ingest_imap")
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
MAX_PER_POLL = 25
MAX_CHARS = 20000


def config() -> dict[str, Any] | None:
    """Réglages lus dans l'environnement ; None si la boîte INSA n'est pas configurée."""
    host, user, password = (os.getenv(k, "").strip() for k in ("INSA_IMAP_HOST", "INSA_IMAP_USER", "INSA_IMAP_PASSWORD"))
    if not (host and user and password):
        return None
    return {"host": host, "user": user, "password": password,
            "port": int(os.getenv("INSA_IMAP_PORT", "993") or 993),
            "folder": os.getenv("INSA_IMAP_FOLDER", "INBOX").strip() or "INBOX",
            "days": int(os.getenv("INSA_IMAP_DAYS", "2") or 2)}


def imap_date(d: date) -> str:
    """Date IMAP (« 11-Nov-2026 »), indépendante de la langue du système."""
    return f"{d.day:02d}-{MONTHS[d.month - 1]}-{d.year}"


def _text(msg: email.message.EmailMessage) -> str:
    part = msg.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    try:
        body = part.get_content()
    except (LookupError, UnicodeError):          # jeu de caractères inconnu : on lit quand même
        body = (part.get_payload(decode=True) or b"").decode("utf-8", "replace")
    if part.get_content_type() == "text/html":
        body = re.sub(r"(?is)<(script|style).*?</\1>", " ", body)
        body = html.unescape(re.sub(r"<[^>]+>", " ", body))
    body = body.replace("\xa0", " ")                 # espaces insécables des mails HTML
    return re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n\n", body)).strip()


def to_text(raw: bytes) -> tuple[str, str]:
    """(texte pour l'extraction, objet) à partir du message brut."""
    msg = email.message_from_bytes(raw, policy=policy.default)
    subject = str(msg.get("subject", "") or "")
    text = f"De : {msg.get('from', '?')}\nObjet : {subject}\n\n{_text(msg)}"
    return text[:MAX_CHARS], subject


def _ok(resp: tuple[str, list]) -> list:
    typ, data = resp
    if typ != "OK":
        raise imaplib.IMAP4.error(f"réponse IMAP {typ}: {data!r}")
    return data


def poll(notify: Notify, cfg: dict[str, Any] | None = None,
         connect: Callable[[str, int], Any] | None = None, today: date | None = None) -> int:
    """Traite les mails INSA non encore vus des derniers jours. Retourne le nombre de nouveaux mails."""
    cfg = cfg or config()
    if cfg is None:
        return 0
    connect = connect or (lambda host, port: imaplib.IMAP4_SSL(host, port, timeout=30))
    conn = connect(cfg["host"], cfg["port"])
    try:
        _ok(conn.login(cfg["user"], cfg["password"]))
        _ok(conn.select(cfg["folder"], readonly=True))
        validity = (conn.response("UIDVALIDITY")[1] or [b"0"])[0]
        validity = validity.decode() if isinstance(validity, bytes) else str(validity or "0")
        since = imap_date((today or datetime.now().date()) - timedelta(days=cfg["days"]))
        uids = b" ".join(x for x in _ok(conn.uid("SEARCH", None, f"SINCE {since}")) if x).split()
        n = 0
        for uid in uids[-MAX_PER_POLL:]:                 # les plus récents, du plus ancien au plus récent
            source = f"insa:{validity}:{uid.decode()}"
            if memory.has_source(source):
                continue
            data = _ok(conn.uid("FETCH", uid, "(BODY.PEEK[])"))
            raw = next((part[1] for part in data if isinstance(part, tuple) and len(part) > 1), None)
            if not raw:
                continue
            text, subject = to_text(raw)
            process_mail(text, subject, source, notify)
            n += 1
        return n
    finally:
        try:
            conn.logout()
        except Exception:  # noqa: BLE001 - la déconnexion ne doit jamais masquer l'erreur d'origine
            log.debug("déconnexion IMAP impossible", exc_info=True)
