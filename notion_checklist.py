"""Cases cochées d'une page Notion -> tâches terminées (sens unique : rien n'est jamais écrit dans Notion).

La page « Certification CompTIA Security+ » liste une case par notion, avec le lien de sa vidéo. Une tâche du
projet regroupe plusieurs notions (leurs liens vidéo sont dans sa description) : elle est terminée quand TOUTES
ses notions sont cochées dans Notion. Désactivé tant que NOTION_CHECKLIST_PAGE n'est pas dans .env ; l'intégration
Notion du bot doit avoir accès à la page (⋯ → Connexions).
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any, Callable, Protocol

from coach.db import Database

log = logging.getLogger("notion_checklist")
API = "https://api.notion.com/v1"
URL = re.compile(r"https?://[^\s<>\"']+")
MAX_BLOCKS = 2000


class BlockReader(Protocol):
    def children(self, block_id: str) -> list[dict[str, Any]]: ...


class HttpBlocks:
    def __init__(self, token: str) -> None:
        import httpx
        self._c = httpx.Client(base_url=API, timeout=20,
                               headers={"Authorization": f"Bearer {token}", "Notion-Version": "2022-06-28"})

    def children(self, block_id: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        cursor = None
        while True:
            params = {"page_size": 100, **({"start_cursor": cursor} if cursor else {})}
            r = self._c.get(f"/blocks/{block_id}/children", params=params)
            r.raise_for_status()
            data = r.json()
            out += data.get("results", [])
            if not data.get("has_more"):
                return out
            cursor = data.get("next_cursor")


def config() -> tuple[str, str] | None:
    """(jeton, identifiant de page) ou None si la lecture des cases n'est pas configurée."""
    token = os.getenv("NOTION_TOKEN", "").strip().strip('"')
    page = os.getenv("NOTION_CHECKLIST_PAGE", "").strip().strip('"')
    return (token, page) if token and page else None


def norm(url: str) -> str:
    return url.strip().rstrip("/.,;:!?)").lower()


def checked_links(reader: BlockReader, page_id: str) -> dict[str, bool]:
    """{lien de la case: cochée ?} pour toutes les cases de la page, sous-blocs compris."""
    found: dict[str, bool] = {}
    todo, seen = [page_id], 0
    while todo and seen < MAX_BLOCKS:
        for b in reader.children(todo.pop()):
            seen += 1
            if b.get("type") == "to_do":
                box = b.get("to_do") or {}
                for rt in box.get("rich_text", []):
                    href = rt.get("href") or ((rt.get("text") or {}).get("link") or {}).get("url")
                    if href:
                        found[norm(href)] = bool(box.get("checked"))
            if b.get("has_children"):
                todo.append(b["id"])
    return found


def sync(db: Database, reader: BlockReader, page_id: str, mark_done: Callable[[str], Any],
         project_name: str = "Security+") -> int:
    """Termine les tâches ouvertes du projet dont toutes les notions sont cochées. Retourne leur nombre."""
    project = next((p for p in db.list_projects() if p.name == project_name), None)
    if project is None:
        return 0
    boxes = checked_links(reader, page_id)
    done = 0
    for t in db.list_project_tasks(project.id):
        if t.status not in ("pending", "scheduled"):
            continue
        mine = [u for u in (norm(u) for u in URL.findall(t.description or "")) if u in boxes]
        if mine and all(boxes[u] for u in mine):
            mark_done(t.id)
            done += 1
            log.info("notion : toutes les cases de « %s » sont cochées, tâche terminée", t.title)
    return done
