"""Synchronisation de la base Notion « Échéances 3IF » avec le coach.

Chaque ligne « À faire » avec une date limite devient une tâche (id stable `nt-<page>`).
Notion reste la source : une ligne passée à « terminé » ferme la tâche locale, et une tâche terminée
côté Telegram repasse à « terminé » dans Notion.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Any, Callable, Protocol
from zoneinfo import ZoneInfo

from coach import dedupe, fmt, project_service
from coach.db import Database
from coach.models import Task

log = logging.getLogger("notion_sync")
TZ = ZoneInfo("Europe/Paris")
API = "https://api.notion.com/v1"
DEFAULT_MINUTES = 60
TODO, DONE = "À faire", "terminé"

SHORT = {  # préfixe court dans le titre de la tâche
    "Modélisation des Processus": "MP", "EPS": "EPS", "Algorithmes et structures de données": "Alg",
    "Modélisation des données": "BD", "Anglais / LV2": "LV", "Génie logiciel et modélisation UML": "GL",
    "Stage 3IF": "Stg", "Programmation en langage C": "C", "Outils de Programmation": "Out",
    "Architecture des circuits numériques": "Arc", "Algorithmique avancée pour l'IA et les graphes": "IA",
    "Projet Personnel et Professionnel année 3": "PPP",
}


@dataclass
class Row:
    page_id: str
    title: str
    subject: str | None
    due: str | None  # AAAA-MM-JJ (ou date-heure ISO)
    status: str | None


class NotionClient(Protocol):
    def fetch_all(self) -> list[Row]: ...
    def set_done(self, page_id: str) -> None: ...


class HttpNotion:
    def __init__(self, token: str, db_id: str) -> None:
        import httpx
        self._c = httpx.Client(
            base_url=API, timeout=20,
            headers={"Authorization": f"Bearer {token}", "Notion-Version": "2022-06-28"})
        self._db = db_id

    def fetch_all(self) -> list[Row]:
        rows: list[Row] = []
        cursor = None
        while True:
            body: dict[str, Any] = {"page_size": 100, **({"start_cursor": cursor} if cursor else {})}
            r = self._c.post(f"/databases/{self._db}/query", json=body)
            r.raise_for_status()
            data = r.json()
            for p in data["results"]:
                pr = p["properties"]
                title = "".join(t["plain_text"] for t in pr["Tâche"]["title"]).strip()
                date = (pr.get("Date limite") or {}).get("date")
                rows.append(Row(
                    page_id=p["id"], title=title,
                    subject=((pr.get("Matière") or {}).get("select") or {}).get("name"),
                    due=date["start"] if date else None,
                    status=((pr.get("Statut") or {}).get("select") or {}).get("name")))
            if not data.get("has_more"):
                return rows
            cursor = data["next_cursor"]

    def set_done(self, page_id: str) -> None:
        r = self._c.patch(f"/pages/{page_id}", json={"properties": {"Statut": {"select": {"name": DONE}}}})
        r.raise_for_status()


def client_from_env() -> HttpNotion | None:
    token, db = os.getenv("NOTION_TOKEN", "").strip().strip('"'), os.getenv("NOTION_DB_ID", "").strip().strip('"')
    return HttpNotion(token, db) if token and db else None


def task_id(page_id: str) -> str:
    return "nt-" + page_id.replace("-", "")[:16]


def _due(value: str) -> datetime:
    if len(value) <= 10:  # date seule : fin de journée
        return datetime.combine(datetime.fromisoformat(value).date(), time(23, 59), tzinfo=TZ)
    d = datetime.fromisoformat(value)
    return d if d.tzinfo else d.replace(tzinfo=TZ)


def _title(row: Row) -> str:
    prefix = SHORT.get(row.subject or "", (row.subject or "")[:3])
    return f"[{prefix}] {row.title}" if prefix else row.title


def _open_twin(db: Database, title: str) -> Task | None:
    """Tâche ouverte venue d'ailleurs (mail, note) qui est la même que cette ligne Notion."""
    others = [t for t in db.list_tasks("pending", "scheduled") if not (t.source_id or "").startswith("notion:")]
    return dedupe.find_task(Task(title=title), others)


def push_done(db: Database, client: NotionClient, task_id_: str) -> bool:
    """Coche tout de suite « terminé » dans Notion pour une tâche qui en vient (sinon : le passage de 15 min)."""
    t = db.get_task(task_id_)
    if t is None or not (t.source_id or "").startswith("notion:"):
        return False
    client.set_done(t.source_id.split(":", 1)[1])  # type: ignore[union-attr]
    return True


def _no_plan_button(buttons: Any) -> Any:
    """Les échéances sont planifiées d'office : on garde seulement « Modifier la durée » et « Ignorer »."""
    return [[b for b in row if not b[1].startswith("plan:")] for row in buttons]


def sync(db: Database, client: NotionClient, notify: Callable[[str, Any], None],
         mark_done: Callable[[str], Any], task_buttons: Callable[[str], Any], sched: Any) -> dict[str, int]:
    rows = client.fetch_all()
    known = {t.id: t for t in db.list_tasks() if (t.source_id or "").startswith("notion:")}
    first_run = not known
    stats = {"new": 0, "updated": 0, "closed": 0, "pushed": 0, "markers": 0, "planned": 0}
    fresh: list[Task] = []
    merged: dict[str, str] = {}  # id Notion -> titre de la tâche déjà connue qu'elle remplace

    for row in rows:
        tid = task_id(row.page_id)
        local = known.get(tid)
        if row.status == DONE:
            if local and local.status in ("pending", "scheduled"):
                mark_done(tid)
                stats["closed"] += 1
            continue
        if not row.due or not row.title:
            continue  # pas de date limite : rien à suivre
        due, title = _due(row.due), _title(row)
        if local is None:
            minutes = DEFAULT_MINUTES
            twin = _open_twin(db, title)
            if twin is not None:  # déjà connue (ex. par un mail) : Notion la remplace, sans doublon dans l'agenda
                sched.drop_task(twin.id)
                minutes = twin.estimated_minutes  # la durée déjà choisie est gardée
                merged[tid] = twin.title
            t = Task(id=tid, title=title, due=due, estimated_minutes=minutes,
                     priority="medium", source_id=f"notion:{row.page_id}")
            t = project_service.attach_by_category(db, t)
            db.upsert_task(t)
            fresh.append(t)
            stats["new"] += 1
        elif local.status in ("pending", "scheduled") and (local.title != title or local.due != due):
            db.upsert_task(project_service.attach_by_category(
                db, local.model_copy(update={"title": title, "due": due})))
            stats["updated"] += 1

    # Telegram -> Notion : tâches terminées ici, encore « À faire » là-bas
    todo_ids = {task_id(r.page_id): r.page_id for r in rows if r.status != DONE}
    for tid, t in known.items():
        if t.status == "done" and tid in todo_ids:
            try:
                client.set_done(todo_ids[tid])
                stats["pushed"] += 1
            except Exception:
                log.warning("Notion : impossible de passer %s à terminé", tid, exc_info=True)

    # marqueurs « journée entière » sur la date limite (créés, déplacés ou retirés)
    now = sched.now()
    current = [t for t in db.list_tasks() if (t.source_id or "").startswith("notion:")]
    for t in current:
        marker = db.get_marker(t.id)
        if t.status in ("done", "dropped") or t.due is None:
            if marker:
                sched.calendar.delete_event(marker["calendar_event_id"])
                db.delete_marker(t.id)
            continue
        day = t.due.date().isoformat()
        if marker is None:
            db.set_marker(t.id, sched.calendar.create_deadline_marker(t), day, t.title)
            stats["markers"] += 1
        elif marker["due_date"] != day or marker["title"] != t.title:
            sched.calendar.update_deadline_marker(marker["calendar_event_id"], t)
            db.set_marker(t.id, marker["calendar_event_id"], day, t.title)

    # bloc de travail (1 h par défaut) placé une fois par tâche et par date limite ;
    # une échéance déjà dépassée est replacée au plus vite (sa vraie date reste en base)
    planned_lines: dict[str, str] = {}
    for t in db.list_tasks("pending"):
        if not (t.source_id or "").startswith("notion:") or t.due is None:
            continue
        if not db.claim_notification(f"autoplan:{t.id}:{t.due.date().isoformat()}"):
            continue
        for old in db.list_blocks(t.id, open_only=True):  # jamais deux blocs ouverts pour la même tâche
            if old.calendar_event_id:
                sched.calendar.delete_event(old.calendar_event_id)
            db.set_block_outcome(old.id, "rescheduled")
        plan_t = t if t.due.date() > now.date() else t.model_copy(update={"due": now + timedelta(days=7)})
        res = sched._place([plan_t])
        stats["planned"] += len(res.blocks)
        if res.blocks:
            planned_lines[t.id] = f"🎯 Bloc placé : {fmt.day_short(res.blocks[0].start)} {fmt.rng(res.blocks[0].start, res.blocks[0].end)}"

    if first_run and fresh:  # import initial : un seul résumé, pas une carte par ligne
        soon = sorted(fresh, key=lambda t: t.due)[:5]  # type: ignore[arg-type,return-value]
        lines = [f"• {fmt.esc(t.title)} · {fmt.day_short(t.due)}" for t in soon]  # type: ignore[arg-type]
        notify(f"📥 <b>{len(fresh)} échéances importées depuis Notion</b>\n"
               f"📌 {stats['markers']} marqueurs ajoutés à l'agenda · 🎯 {stats['planned']} blocs de "
               f"{fmt.dur(DEFAULT_MINUTES)} placés\n\nLes plus proches :\n" + "\n".join(lines)
               + "\n\n<i>Détail : /today, /tasks, /semaine</i>", None)
    else:
        if not fresh and (stats["markers"] or stats["planned"]):  # mise à niveau des tâches déjà connues
            notify("🔄 <b>Échéances Notion mises à jour dans l'agenda</b>" + "\n" +
                   f"📌 {stats['markers']} marqueurs · 🎯 {stats['planned']} blocs de {fmt.dur(DEFAULT_MINUTES)} placés"
                   + "\n" + "\n" + "<i>Détail : /today, /semaine</i>", None)
        for t in fresh:
            card = fmt.task_card(t, "📥 <b>Nouvelle échéance Notion</b>")
            if t.id in merged:
                card += f"\n🔗 Remplace « {fmt.esc(merged[t.id])} », déjà connue : pas de doublon."
            notify(card + ("\n" + planned_lines[t.id] if t.id in planned_lines else ""),
                   _no_plan_button(task_buttons(t.id)))
    return stats
