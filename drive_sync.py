"""Google Drive : dossier « Second cerveau », Inbox, classement et indexation.

    Second cerveau/
      00_Inbox/   <- dépôt vrac ; classé puis déplacé
      Cours/<matière>/   Admin/ (privé)   Perso/ (privé)   Recettes/   Divers/ (privé)

La sensibilité vient du dossier. Les fichiers restent sur Drive ; seul le texte extrait est indexé
localement (memory.py). Classement : mots-clés d'abord, puis LLM si ALLOW_CLOUD_FOR_DOCS=true,
sinon on demande à l'utilisateur (boutons Telegram).
"""
from __future__ import annotations

import functools
import io
import json
import threading
import logging
import os
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import memory
from llm_router import router

log = logging.getLogger("drive_sync")

INBOX = "00_Inbox"
FOLDERS = {  # clé courte (callback Telegram) -> (nom du dossier, sensibilité)
    "cours": ("Cours", "public"),
    "admin": ("Admin", "private"),
    "perso": ("Perso", "private"),
    "recettes": ("Recettes", "public"),
    "divers": ("Divers", "private"),
}
FOLDER_MIME = "application/vnd.google-apps.folder"
GDOC_MIME = "application/vnd.google-apps.document"
CACHE = Path("data") / "drive_folders.json"
MIN_CONFIDENCE = 0.6

KEYWORDS = {
    "admin": ["facture", "attestation", "impot", "banque", "releve", "assurance", "ordonnance", "mutuelle",
              "contrat", "loyer", "caf", "securite sociale", "carte vitale", "inscription", "scolarite"],
    "recettes": ["recette", "ingredients", "cuisson", "gateau", "plat"],
    "cours": ["cours", "td ", "tp ", "polycopie", "support", "chapitre", "examen", "partiel", "cm ", "amphi"],
    "perso": ["journal", "perso", "ide ", "voeux"],
}


def _norm(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s.lower()) if unicodedata.category(c) != "Mn")


@dataclass
class Result:
    doc_id: str
    folder_key: str | None  # None = non classé (reste dans l'Inbox)
    subfolder: str | None
    sensitivity: str
    file_id: str
    filename: str


def sensitivity_of(folder_key: str | None) -> str:
    return FOLDERS[folder_key][1] if folder_key in FOLDERS else "private"


def classify_by_keywords(filename: str, text: str = "") -> str | None:
    blob = _norm(f"{filename} {text[:500]}") + " "
    hits = {k for k, words in KEYWORDS.items() if any(w in blob for w in words)}
    return hits.pop() if len(hits) == 1 else None


def cloud_allowed_for_docs() -> bool:
    return os.getenv("ALLOW_CLOUD_FOR_DOCS", "false").strip().lower() == "true"


def classify(filename: str, text: str) -> tuple[str | None, str | None]:
    """(clé de dossier, sous-dossier) ou (None, None) si on ne sait pas (=> on demande)."""
    key = classify_by_keywords(filename, text)
    if key and key != "cours":
        return key, None
    try:
        raw = router.complete(
            f"Nom du fichier : {filename}\nExtrait :\n{text[:1500]}",
            sensitivity="public" if cloud_allowed_for_docs() else "private",
            system=("Classe ce document. Réponds UNIQUEMENT en JSON : "
                    '{"folder":"cours|admin|perso|recettes|divers","subfolder":str|null,"confidence":0..1}. '
                    "subfolder = nom court de la matière si folder=cours, sinon null."),
            json_mode=True)
        data = json.loads(raw[raw.index("{"): raw.rindex("}") + 1])
        if data.get("folder") in FOLDERS and float(data.get("confidence", 0)) >= MIN_CONFIDENCE:
            sub = re.sub(r"[^\w \-]", "", str(data.get("subfolder") or "")).strip()[:40] or None
            return data["folder"], sub if data["folder"] == "cours" else None
    except Exception:
        log.warning("classification LLM impossible pour %s", filename, exc_info=True)
    return (key, None) if key else (None, None)


_drive: "Drive | None" = None


def get_drive() -> "Drive":  # pragma: no cover - réseau / OAuth
    global _drive
    if _drive is None:
        _drive = Drive(build_service())
    return _drive


# --- accès Drive ---------------------------------------------------------------

def build_service() -> Any:  # pragma: no cover - réseau / OAuth
    from googleapiclient.discovery import build
    from coach.calendar_client import credentials_from_env
    return build("drive", "v3", credentials=credentials_from_env(), cache_discovery=False)


def _q(name: str) -> str:
    return name.replace("\\", "\\\\").replace("'", "\\'")


class Drive:
    def __init__(self, service: Any, root_name: str | None = None) -> None:
        self.svc = service
        self._lock = threading.RLock()  # client HTTP de Google non thread-safe (bot + job Drive)
        self.root_name = root_name or os.getenv("DRIVE_ROOT_NAME", "Second cerveau")
        self._ids: dict[str, str] = {}
        if CACHE.exists():
            try:
                self._ids = json.loads(CACHE.read_text(encoding="utf-8"))
            except Exception:
                self._ids = {}

    def _save(self) -> None:
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps(self._ids), encoding="utf-8")

    def folder(self, path: str) -> str:
        """Id du dossier `path` (relatif à la racine), créé si besoin."""
        full = f"{self.root_name}/{path}".strip("/")
        if full in self._ids:
            return self._ids[full]
        parent = None
        so_far = ""
        for part in full.split("/"):
            so_far = f"{so_far}/{part}".strip("/")
            if so_far in self._ids:
                parent = self._ids[so_far]
                continue
            q = f"name='{_q(part)}' and mimeType='{FOLDER_MIME}' and trashed=false"
            q += f" and '{parent}' in parents" if parent else " and 'root' in parents"
            found = self.svc.files().list(q=q, fields="files(id)", pageSize=1).execute().get("files", [])
            if found:
                fid = found[0]["id"]
            else:
                body = {"name": part, "mimeType": FOLDER_MIME, **({"parents": [parent]} if parent else {})}
                fid = self.svc.files().create(body=body, fields="id").execute()["id"]
            self._ids[so_far] = parent = fid
        self._save()
        return self._ids[full]

    def ensure_tree(self) -> None:
        self.folder(INBOX)
        for name, _ in FOLDERS.values():
            self.folder(name)

    def list_files(self, folder_id: str) -> list[dict]:
        out, token = [], None
        while True:
            r = self.svc.files().list(
                q=f"'{folder_id}' in parents and trashed=false", pageToken=token, pageSize=100,
                fields="nextPageToken, files(id,name,mimeType,parents)").execute()
            out += r.get("files", [])
            token = r.get("nextPageToken")
            if not token:
                return out

    def upload(self, path: str, filename: str, folder_id: str) -> str:
        from googleapiclient.http import MediaFileUpload
        return self.svc.files().create(
            body={"name": filename, "parents": [folder_id]}, media_body=MediaFileUpload(path),
            fields="id").execute()["id"]

    def read_text(self, f: dict) -> str:
        mime, fid, name = f.get("mimeType", ""), f["id"], f["name"].lower()
        try:
            if mime == GDOC_MIME:
                return self.svc.files().export(fileId=fid, mimeType="text/plain").execute().decode("utf-8", "replace")
            if name.endswith((".txt", ".md")):
                return self._download(fid).decode("utf-8", "replace")
            if name.endswith(".pdf"):
                from pypdf import PdfReader
                return "\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(self._download(fid))).pages)
        except Exception:
            log.warning("lecture impossible : %s", f["name"], exc_info=True)
        return ""  # photo, format inconnu : on indexe le nom seulement

    def _download(self, fid: str) -> bytes:
        from googleapiclient.http import MediaIoBaseDownload
        buf = io.BytesIO()
        dl = MediaIoBaseDownload(buf, self.svc.files().get_media(fileId=fid))
        done = False
        while not done:
            _, done = dl.next_chunk()
        return buf.getvalue()

    def move(self, file_id: str, to_folder: str, from_folder: str) -> None:
        self.svc.files().update(fileId=file_id, addParents=to_folder, removeParents=from_folder,
                                fields="id").execute()


def _locked(fn: Any) -> Any:
    @functools.wraps(fn)
    def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
        with self._lock:
            return fn(self, *args, **kwargs)
    return wrapper


for _name in ("folder", "list_files", "upload", "read_text", "move"):
    setattr(Drive, _name, _locked(getattr(Drive, _name)))


# --- traitement --------------------------------------------------------------------

def process_file(drive: Drive, f: dict, from_folder: str, classify_and_move: bool = True,
                 folder_key: str | None = None) -> Result | None:
    """Indexe un fichier Drive ; depuis l'Inbox, le classe et le déplace. None si déjà indexé."""
    src = f"drive:{f['id']}"
    if memory.has_source(src):
        return None
    text = drive.read_text(f)
    sub = None
    if classify_and_move:
        folder_key, sub = classify(f["name"], text)
        if folder_key:
            path = FOLDERS[folder_key][0] + (f"/{sub}" if sub else "")
            drive.move(f["id"], drive.folder(path), from_folder)
    sens = sensitivity_of(folder_key)
    doc_id = memory.add_drive_doc(f["name"], text, sens, src, folder_key, sub)
    return Result(doc_id, folder_key, sub, sens, f["id"], f["name"])


def process_inbox(drive: Drive) -> list[Result]:
    inbox = drive.folder(INBOX)
    out = []
    for f in drive.list_files(inbox):
        if f["mimeType"] == FOLDER_MIME:
            continue
        r = process_file(drive, f, inbox)
        if r:
            out.append(r)
    return out


def index_untracked(drive: Drive) -> list[Result]:
    """Indexe les fichiers déposés à la main dans les dossiers déjà rangés (sensibilité = dossier)."""
    out: list[Result] = []

    def walk(folder_id: str, key: str | None) -> None:
        for f in drive.list_files(folder_id):
            if f["mimeType"] == FOLDER_MIME:
                walk(f["id"], key)
            else:
                r = process_file(drive, f, folder_id, classify_and_move=False, folder_key=key)
                if r:
                    out.append(r)

    for key, (name, _) in FOLDERS.items():
        walk(drive.folder(name), key)
    return out


def sync(drive: Drive) -> list[Result]:
    drive.ensure_tree()
    return process_inbox(drive) + index_untracked(drive)
