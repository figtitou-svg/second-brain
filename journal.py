"""Journal Google Docs : tout ce que l'utilisateur écrit ou dit au bot y est conservé, jour par jour.

Un seul document « Journal Second cerveau » dans le dossier Drive « Second cerveau » :

    vendredi 16 octobre 2026                      <- titre (un par jour)
    14:32 · texte — Le code du garage est 4721
    14:35 · vocal — Appeler la banque lundi

Rien n'est jamais perdu : chaque entrée est d'abord écrite dans une file locale (data/journal_pending.jsonl),
puis envoyée à Google Docs ; si l'envoi échoue (réseau, autorisation), elle reste en file et repart au
prochain passage du job.
"""
from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from coach import fmt

log = logging.getLogger("journal")
TZ = ZoneInfo("Europe/Paris")
DOC_NAME = "Journal Second cerveau"
DOC_MIME = "application/vnd.google-apps.document"
PENDING = Path("data") / "journal_pending.jsonl"
STATE = Path("data") / "journal.json"
_lock = threading.RLock()


@dataclass
class Entry:
    ts: datetime
    source: str  # texte | vocal | note
    text: str


def render(entries: list[Entry], last_day: str | None) -> tuple[str, list[tuple[int, int]], str | None]:
    """(texte à ajouter, positions (début, fin) des titres de jour, dernier jour écrit). Fonction pure."""
    out, headers, day = "", [], last_day
    for e in entries:
        d = e.ts.astimezone(TZ)
        if d.date().isoformat() != day:
            title = f"{fmt.day_long(d)} {d.year}"
            headers.append((len(out), len(out) + len(title) + 1))
            out += f"{title}\n"
            day = d.date().isoformat()
        out += f"{fmt.hm(d)} · {e.source} — {e.text.strip()}\n"
    return out, headers, day


# --- file d'attente locale -----------------------------------------------------------------

def add(text: str, source: str = "texte", now: datetime | None = None, flush: bool = True) -> None:
    """Enregistre l'entrée dans la file (jamais perdue) puis tente l'envoi vers Google Docs."""
    text = text.strip()
    if not text:
        return
    ts = (now or datetime.now(TZ)).isoformat()
    with _lock:
        PENDING.parent.mkdir(parents=True, exist_ok=True)
        with PENDING.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": ts, "source": source, "text": text}, ensure_ascii=False) + "\n")
    if flush:
        try:
            flush_pending()
        except Exception:  # noqa: BLE001 - l'entrée reste en file, le job réessaiera
            log.warning("journal : envoi différé", exc_info=True)


def pending() -> list[Entry]:
    if not PENDING.exists():
        return []
    out = []
    for line in PENDING.read_text(encoding="utf-8").splitlines():
        if line.strip():
            d = json.loads(line)
            out.append(Entry(datetime.fromisoformat(d["ts"]), d["source"], d["text"]))
    return out


def _state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


# --- Google Docs ---------------------------------------------------------------------------

def _services() -> tuple[Any, Any]:  # pragma: no cover - réseau / OAuth
    from googleapiclient.discovery import build

    import drive_sync
    from coach.calendar_client import credentials_from_env
    docs = build("docs", "v1", credentials=credentials_from_env(), cache_discovery=False)
    return drive_sync.get_drive(), docs


def ensure_doc(drive: Any, state: dict) -> str:
    """Id du document (retrouvé par son nom dans le dossier, créé s'il n'existe pas)."""
    if state.get("doc_id"):
        return state["doc_id"]
    root = drive.folder("")
    found = drive.svc.files().list(
        q=f"name='{DOC_NAME}' and mimeType='{DOC_MIME}' and '{root}' in parents and trashed=false",
        fields="files(id)", pageSize=1).execute().get("files", [])
    doc_id = found[0]["id"] if found else drive.svc.files().create(
        body={"name": DOC_NAME, "mimeType": DOC_MIME, "parents": [root]}, fields="id").execute()["id"]
    state["doc_id"] = doc_id
    return doc_id


def flush_pending(services: tuple[Any, Any] | None = None) -> int:
    """Envoie toutes les entrées en attente en une seule opération. Retourne leur nombre."""
    with _lock:
        entries = pending()
        if not entries:
            return 0
        drive, docs = services or _services()
        state = _state()
        doc_id = ensure_doc(drive, state)
        text, headers, last_day = render(entries, state.get("last_day"))
        doc = docs.documents().get(documentId=doc_id, fields="body/content/endIndex").execute()
        start = doc["body"]["content"][-1]["endIndex"] - 1
        requests: list[dict] = [
            {"insertText": {"location": {"index": start}, "text": text}},
            {"updateParagraphStyle": {"range": {"startIndex": start, "endIndex": start + len(text)},
                                      "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
                                      "fields": "namedStyleType"}},
        ]
        requests += [{"updateParagraphStyle": {"range": {"startIndex": start + s, "endIndex": start + e},
                                               "paragraphStyle": {"namedStyleType": "HEADING_2"},
                                               "fields": "namedStyleType"}} for s, e in headers]
        docs.documents().batchUpdate(documentId=doc_id, body={"requests": requests}).execute()
        state["last_day"] = last_day
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(state), encoding="utf-8")
        PENDING.write_text("", encoding="utf-8")  # seulement après l'écriture réussie
        return len(entries)
