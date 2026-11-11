"""Boîte mail INSA (Zimbra) en IMAP, lecture seule : mêmes cartes et même confidentialité que Gmail.

La boîte est ouverte en lecture seule (EXAMINE) et les messages lus avec BODY.PEEK : rien n'est marqué comme lu,
déplacé ou supprimé. Identifiants dans .env uniquement (INSA_IMAP_HOST, INSA_IMAP_USER, INSA_IMAP_PASSWORD).
"""
from __future__ import annotations

import base64
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
            # dossiers lus, séparés par des virgules (les filtres Zimbra rangent les mails ; un dossier absent
            # de la liste, comme « Comptes et sécurité », n'est jamais lu)
            "folders": [f.strip() for f in (os.getenv("INSA_IMAP_FOLDERS") or os.getenv("INSA_IMAP_FOLDER") or "INBOX").split(",")
                        if f.strip()] or ["INBOX"],
            "days": int(os.getenv("INSA_IMAP_DAYS", "2") or 2),
            # expéditeurs jamais lus (ni tâche, ni mémoire) : morceaux d'adresse, séparés par des virgules
            "skip": [s.strip().lower() for s in (os.getenv("INSA_IMAP_SKIP") or DEFAULT_SKIP).split(",") if s.strip()]}


# alertes de compte : codes de connexion, MFA, alertes de sécurité (ce n'est jamais une tâche)
DEFAULT_SKIP = "mfa-noreply@,notify@updates.notion,nepasrepondre@sport-u.com"


def skipped(header: bytes | None, skip: list[str]) -> bool:
    """Le mail vient d'un expéditeur à ignorer (alerte de compte…)."""
    if not header or not skip:
        return False
    frm = str(email.message_from_bytes(header, policy=policy.default).get("from", "") or "").lower()
    return any(s in frm for s in skip)


def mutf7(name: str) -> str:
    """Nom de dossier en UTF-7 modifié (RFC 3501), entre guillemets : « Vie INSA et département » passe tel quel."""
    out, run = [], ""

    def flush() -> None:
        nonlocal run
        if run:
            b = base64.b64encode(run.encode("utf-16-be")).decode().rstrip("=").replace("/", ",")
            out.append(f"&{b}-")
            run = ""

    for ch in name:
        if 0x20 <= ord(ch) <= 0x7E:
            flush()
            out.append("&-" if ch == "&" else ch)
        else:
            run += ch
    flush()
    quoted = "".join(out).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{quoted}"'


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


def _first(data: list) -> bytes | None:
    return next((part[1] for part in data if isinstance(part, tuple) and len(part) > 1), None)


def message_key(header: bytes | None) -> str | None:
    """Clé stable d'un mail (son Message-ID) : un mail rangé dans un autre dossier n'est pas traité deux fois."""
    if not header:
        return None
    mid = str(email.message_from_bytes(header, policy=policy.default).get("message-id", "") or "").strip().strip("<>").strip()
    return f"insa:mid:{mid}" if mid else None


def _poll_folder(conn: Any, folder: str, since: str, notify: Notify, skip: list[str] | None = None) -> int:
    validity = (conn.response("UIDVALIDITY")[1] or [b"0"])[0]
    validity = validity.decode() if isinstance(validity, bytes) else str(validity or "0")
    uids = b" ".join(x for x in _ok(conn.uid("SEARCH", None, f"SINCE {since}")) if x).split()
    n = 0
    for uid in uids[-MAX_PER_POLL:]:                     # les plus récents, du plus ancien au plus récent
        # ancienne clé (dossier + numéro) : gardée pour la Réception, déjà utilisée avant les dossiers
        legacy = f"insa:{validity}:{uid.decode()}" if folder == "INBOX" else f"insa:{folder}:{validity}:{uid.decode()}"
        if memory.has_source(legacy):
            continue
        header = _first(_ok(conn.uid("FETCH", uid, "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID FROM)])")))
        if skipped(header, skip or []):
            continue
        key = message_key(header)
        if key and memory.has_source(key):
            continue
        raw = _first(_ok(conn.uid("FETCH", uid, "(BODY.PEEK[])")))
        if not raw:
            continue
        text, subject = to_text(raw)
        process_mail(text, subject, key or legacy, notify)
        n += 1
    return n


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
        since = imap_date((today or datetime.now().date()) - timedelta(days=cfg["days"]))
        n = 0
        for folder in cfg["folders"]:
            typ, _ = conn.select(mutf7(folder) if folder != "INBOX" else folder, readonly=True)
            if typ != "OK":                              # dossier pas (encore) créé : on passe au suivant
                log.warning("mail INSA : dossier %r introuvable", folder)
                continue
            n += _poll_folder(conn, folder, since, notify, cfg.get("skip"))
        return n
    finally:
        try:
            conn.logout()
        except Exception:  # noqa: BLE001 - la déconnexion ne doit jamais masquer l'erreur d'origine
            log.debug("déconnexion IMAP impossible", exc_info=True)
