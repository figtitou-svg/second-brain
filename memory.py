"""Mémoire minimale (SQLite FTS5) : notes, documents, recherche avec source.

Remplace temporairement le fork second-brain : même interface (voir bot.ports.MemoryPort).
Contenu `private` -> réponse générée par le modèle local uniquement (via llm_router).
"""
from __future__ import annotations

import logging
import os
import re
import sqlite3
import threading
import uuid
from datetime import datetime
from pathlib import Path

from bot.ports import Answer
from llm_router import router

log = logging.getLogger("memory")
DB_PATH = Path("data") / "memory.sqlite"
_last: dict = {}  # doc_id -> drive_sync.Result (dernier classement, pour describe/move)
_lock = threading.RLock()
_conn: sqlite3.Connection | None = None


def _db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        _conn.executescript(
            """CREATE TABLE IF NOT EXISTS docs (
                 id TEXT PRIMARY KEY, title TEXT, kind TEXT, sensitivity TEXT, source TEXT,
                 created_at TEXT, body TEXT, folder TEXT, sub TEXT);
               CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts USING fts5(
                 title, body, content='docs', content_rowid='rowid', tokenize='unicode61 remove_diacritics 2');"""
        )
        for col in ("folder", "sub"):  # migration des bases créées avant l'arrivée de Drive
            try:
                _conn.execute(f"ALTER TABLE docs ADD COLUMN {col} TEXT")
            except sqlite3.OperationalError:
                pass
    return _conn


def _add(title: str, body: str, kind: str, sensitivity: str, source: str,
         folder: str | None = None, sub: str | None = None) -> str:
    doc_id = uuid.uuid4().hex[:8]
    with _lock:
        c = _db()
        cur = c.execute(
            "INSERT INTO docs (id,title,kind,sensitivity,source,created_at,body,folder,sub) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (doc_id, title, kind, sensitivity, source, datetime.now().isoformat(), body, folder, sub))
        c.execute("INSERT INTO docs_fts(rowid,title,body) VALUES (?,?,?)", (cur.lastrowid, title, body))
        c.commit()
    return doc_id


def add_note(text: str, sensitivity: str, source: str) -> str:
    return _add(text.strip().splitlines()[0][:60] if text.strip() else "note", text, "note", sensitivity, source)


def _pdf_text(path: str) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        return ""
    return "\n".join((p.extract_text() or "") for p in PdfReader(path).pages)


def add_drive_doc(filename: str, text: str, sensitivity: str, source: str,
                  folder: str | None, sub: str | None) -> str:
    return _add(filename, text or filename, "drive", sensitivity, source, folder, sub)


def add_document(path: str, filename: str, sensitivity: str, kind: str) -> str:
    """Dépose dans l'Inbox Drive puis classe/indexe tout de suite ; repli local si Drive est inactif."""
    if os.getenv("DRIVE_ENABLED", "true").strip().lower() == "true":
        try:
            import drive_sync
            d = drive_sync.get_drive()
            inbox = d.folder(drive_sync.INBOX)
            fid = d.upload(path, filename, inbox)
            res = drive_sync.process_file(d, {"id": fid, "name": filename, "mimeType": ""}, inbox)
            if res:
                _last[res.doc_id] = res
                return res.doc_id
        except Exception:
            log.warning("Drive indisponible, indexation locale seule", exc_info=True)
    body = _pdf_text(path) if kind == "pdf" else ""  # photos : pas d'OCR pour l'instant
    return _add(filename, body or filename, kind, sensitivity, "telegram")


def set_sensitivity(doc_id: str, sensitivity: str) -> None:
    with _lock:
        c = _db()
        c.execute("UPDATE docs SET sensitivity=? WHERE id=?", (sensitivity, doc_id))
        c.commit()


STOPWORDS = {"les", "des", "une", "que", "qui", "quoi", "est", "ce", "cet", "cette", "ces", "aux", "dans", "pour",
             "avec", "sur", "mon", "mes", "ton", "tes", "aujourd", "hui", "demain", "ici", "faut", "dois", "suis",
             "aprem", "apres", "midi", "soir", "matin", "semaine", "quel", "quels", "quelle", "quand", "comment",
             "combien", "pas", "plus", "tout", "tous", "fait", "faire", "ai", "ait", "avoir"}


def _search(question: str, k: int = 5) -> list[tuple[str, str, str]]:
    words = [w for w in re.findall(r"\w{3,}", question.lower()) if w not in STOPWORDS]
    if not words:
        return []
    q = " OR ".join(f'"{w}"' for w in words)
    with _lock:
        rows = _db().execute(
            """SELECT d.title, d.sensitivity, substr(d.body,1,3000) FROM docs_fts f
               JOIN docs d ON d.rowid=f.rowid WHERE docs_fts MATCH ? ORDER BY bm25(docs_fts) LIMIT ?""",
            (q, k)).fetchall()
    return rows


_EXCERPT = 240
_COLS = "id,title,kind,sensitivity,source,created_at,folder,body"


def _flat(text: str | None, limit: int = _EXCERPT) -> str:
    return " ".join((text or "").split())[:limit]


def _parse_dt(value: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if value else None
    except (TypeError, ValueError):
        return None


def _row(r, with_body: bool = False) -> dict:
    body = r[7] or ""
    d = {"id": r[0], "title": r[1], "kind": r[2], "sensitivity": r[3], "source": r[4],
         "created_at": _parse_dt(r[5]), "folder": r[6], "excerpt": _flat(body)}
    if with_body:
        d["body"] = body
    return d


def recent_docs(limit: int = 50, kind: str | None = None) -> list[dict]:
    """Lecture seule : documents les plus récents d'abord (created_at puis rowid)."""
    sql, params = f"SELECT {_COLS} FROM docs", ()
    if kind:
        sql, params = sql + " WHERE kind=?", (kind,)
    try:
        with _lock:
            rows = _db().execute(sql + " ORDER BY created_at DESC, rowid DESC LIMIT ?",
                                 (*params, max(1, min(int(limit), 500)))).fetchall()
    except sqlite3.Error:
        log.warning("lecture des notes impossible", exc_info=True)
        return []
    return [_row(r) for r in rows]


def get_doc(doc_id: str) -> dict | None:
    """Lecture seule : un document avec son texte intégral, ou None."""
    try:
        with _lock:
            r = _db().execute(f"SELECT {_COLS} FROM docs WHERE id=?", (doc_id,)).fetchone()
    except sqlite3.Error:
        log.warning("lecture d'une note impossible", exc_info=True)
        return None
    return _row(r, with_body=True) if r else None


def search_docs(query: str, k: int = 20) -> list[dict]:
    """Recherche plein texte en lecture seule ; le MATCH n'est fait que de mots échappés entre guillemets."""
    words = [w for w in re.findall(r"\w{3,}", (query or "")[:500].lower()) if w not in STOPWORDS][:12]
    if not words:
        return []
    match = " OR ".join('"' + w.replace('"', '""') + '"' for w in words)
    try:
        with _lock:
            rows = _db().execute(
                """SELECT d.id,d.title,d.kind,d.sensitivity,d.source,d.created_at,d.folder,substr(d.body,1,3000)
                   FROM docs_fts f JOIN docs d ON d.rowid=f.rowid WHERE docs_fts MATCH ?
                   ORDER BY bm25(docs_fts) LIMIT ?""", (match, max(1, min(int(k), 100)))).fetchall()
    except sqlite3.Error:
        log.warning("recherche de notes impossible", exc_info=True)
        return []
    out = []
    for r in rows:
        d = _row(r)
        body = r[7] or ""
        low = body.lower()
        first = min((i for i in (low.find(w) for w in words) if i >= 0), default=-1)
        if first > 60:
            d["excerpt"] = "…" + _flat(body[first - 60:], _EXCERPT - 1)
        out.append(d)
    return out


def _now_line() -> str:
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from coach import fmt
    now = datetime.now(ZoneInfo("Europe/Paris"))
    return f"Date et heure actuelles : {fmt.day_long(now)} {now.year}, {fmt.hm(now)} (Europe/Paris)."


SYSTEM = ("Tu es l'assistant personnel de l'utilisateur. Réponds en français, de façon concise, uniquement à partir "
          "du contexte fourni (agenda, échéances, notes). Si l'information manque, dis-le. Pour les notes, cite les "
          "numéros de sources [n] utilisés. 'Cet après-midi' = 12h-18h, 'ce soir' = 18h-23h, 'demain' = le jour suivant la date actuelle.")


def ask(question: str, context: str | None = None) -> Answer:
    """`context` : agenda + échéances en texte (fourni par le coach), avec la date actuelle."""
    hits = _search(question)
    if not hits and not context:
        return Answer("Je n'ai rien trouvé dans ta mémoire à ce sujet.")

    def build(selected: list) -> str:
        parts = [context or _now_line()]
        if selected:
            notes = "\n\n".join(f"[{i}] {t}\n{b}" for i, (t, _, b) in enumerate(selected, 1))
            parts.append(f"Notes et documents :\n{notes}")
        return "\n\n".join(parts) + f"\n\nQuestion : {question}"

    sens = "private" if any(h[1] == "private" for h in hits) else (
        "public" if router.cloud_allowed_for_notes() or not context else "private")
    note = ""
    try:
        text = router.complete(build(hits), sensitivity=sens, system=SYSTEM)  # type: ignore[arg-type]
        used = hits
    except router.PrivateModelUnavailableError:
        # modèle local absent : on répond sans les sources privées plutôt que de les envoyer au cloud
        used = [h for h in hits if h[1] != "private"]
        if not used and not context:
            raise
        text = router.complete(build(used), sensitivity="public", system=SYSTEM)
        note = "\n\n(Des sources privées ont été ignorées : le modèle local n'est pas disponible.)"
    sources = (["Agenda"] if context else []) + [t for t, _, _ in used]
    return Answer(text + note, sources)


def add_mail(text: str, subject: str, source: str, sensitivity: str = "private") -> str:
    return _add(subject, text, "mail", sensitivity, source)


def has_source(source: str) -> bool:
    with _lock:
        return _db().execute("SELECT 1 FROM docs WHERE source=? LIMIT 1", (source,)).fetchone() is not None


def describe(doc_id: str) -> tuple[str, list[tuple[str, str]]]:
    """(message, boutons) pour Telegram après un dépôt de document."""
    import drive_sync
    r = _last.get(doc_id)
    if r is None:
        return "Document indexé (Drive indisponible : copie locale seulement).", []
    if r.folder_key:
        where = drive_sync.FOLDERS[r.folder_key][0] + (f"/{r.subfolder}" if r.subfolder else "")
        label = "privé" if r.sensitivity == "private" else "public"
        return f"{r.filename} rangé dans {where} ({label}).", []
    buttons = [(drive_sync.FOLDERS[k][0], f"mv:{doc_id}:{k}") for k in drive_sync.FOLDERS]
    return f"{r.filename} est dans l'Inbox (privé pour l'instant). Dans quel dossier ?", buttons


def move(doc_id: str, folder_key: str) -> str:
    """Choix manuel du dossier (bouton Telegram) : déplace sur Drive et met à jour la sensibilité."""
    import drive_sync
    r = _last.get(doc_id)
    if r is None or folder_key not in drive_sync.FOLDERS:
        return "Document introuvable."
    d = drive_sync.get_drive()
    name, sens = drive_sync.FOLDERS[folder_key]
    d.move(r.file_id, d.folder(name), d.folder(drive_sync.INBOX))
    with _lock:
        c = _db()
        c.execute("UPDATE docs SET sensitivity=?, folder=? WHERE id=?", (sens, folder_key, doc_id))
        c.commit()
    r.folder_key, r.sensitivity = folder_key, sens
    return f"{r.filename} rangé dans {name} ({'privé' if sens == 'private' else 'public'})."
