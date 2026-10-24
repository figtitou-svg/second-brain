"""Persistance SQLite (schéma PROJECT.md §7.5 + 2 tables techniques)."""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import date, datetime
from pathlib import Path

from coach.models import Block, Event, Milestone, Project, Suggestion, Task

DEFAULT_DB_PATH = Path("data") / "coach.sqlite"

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  due TEXT,
  estimated_minutes INTEGER,
  priority TEXT CHECK (priority IN ('low','medium','high')),
  status TEXT CHECK (status IN ('pending','scheduled','done','dropped')) DEFAULT 'pending',
  source_id TEXT,
  reschedule_count INTEGER DEFAULT 0,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  not_before TEXT,
  boost INTEGER DEFAULT 0,
  project_id TEXT,
  milestone_id TEXT
);
CREATE TABLE IF NOT EXISTS blocks (
  id TEXT PRIMARY KEY,
  task_id TEXT REFERENCES tasks(id),
  calendar_event_id TEXT,
  start TEXT NOT NULL,
  end TEXT NOT NULL,
  outcome TEXT CHECK (outcome IN ('done','not_finished','rescheduled','missed')),
  style TEXT
);
CREATE TABLE IF NOT EXISTS deadline_markers (
  task_id TEXT PRIMARY KEY,
  calendar_event_id TEXT NOT NULL,
  due_date TEXT,
  title TEXT
);
CREATE TABLE IF NOT EXISTS events (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  start TEXT NOT NULL,
  end TEXT NOT NULL,
  location TEXT,
  source_id TEXT,
  calendar_event_id TEXT,
  status TEXT DEFAULT 'proposed'   -- proposed | added | ignored
);
-- techniques (hors §7.5) : cache d'extraction et dédoublonnage des notifications
CREATE TABLE IF NOT EXISTS extraction_cache (
  key TEXT PRIMARY KEY,
  payload TEXT NOT NULL,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS notifications (
  key TEXT PRIMARY KEY,
  sent_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS subject_weights (
  subject TEXT PRIMARY KEY,
  weight REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS projects (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  category TEXT,
  status TEXT CHECK (status IN ('active','paused','done')) DEFAULT 'active',
  deadline TEXT,
  profile TEXT DEFAULT '{}',
  created_at TEXT,
  last_activity TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_projects_category
  ON projects(category) WHERE category IS NOT NULL AND status != 'done';
CREATE TABLE IF NOT EXISTS milestones (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  title TEXT NOT NULL,
  position INTEGER NOT NULL DEFAULT 0,
  due TEXT,
  status TEXT CHECK (status IN ('open','done')) DEFAULT 'open',
  done_at TEXT
);
CREATE TABLE IF NOT EXISTS project_examples (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT NOT NULL,
  project_id TEXT NOT NULL,
  milestone_id TEXT
);
CREATE TABLE IF NOT EXISTS project_proposals (
  task_id TEXT PRIMARY KEY,
  project_id TEXT,
  milestone_id TEXT,
  state TEXT DEFAULT 'proposed'
);
CREATE TABLE IF NOT EXISTS project_draft (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  stage TEXT NOT NULL,
  profile TEXT NOT NULL DEFAULT '{}',
  questions_asked INTEGER NOT NULL DEFAULT 0,
  pending TEXT,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS project_suggestions (
  id TEXT PRIMARY KEY,
  batch TEXT NOT NULL,
  project_id TEXT NOT NULL,
  milestone_id TEXT,
  title TEXT NOT NULL,
  estimated_minutes INTEGER NOT NULL DEFAULT 30,
  state TEXT NOT NULL DEFAULT 'proposed' CHECK (state IN ('proposed','added','planned','dismissed')),
  created_at TEXT NOT NULL
);
"""


def _dt(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s) if s else None


class Database:
    def __init__(self, path: str | Path = DEFAULT_DB_PATH) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(SCHEMA)
        for ddl in ("ALTER TABLE tasks ADD COLUMN not_before TEXT",  # migration des bases plus anciennes
                    "ALTER TABLE tasks ADD COLUMN boost INTEGER DEFAULT 0",
                    "ALTER TABLE blocks ADD COLUMN style TEXT",
                    "ALTER TABLE tasks ADD COLUMN project_id TEXT",
                    "ALTER TABLE tasks ADD COLUMN milestone_id TEXT"):
            try:
                self._conn.execute(ddl)
            except sqlite3.OperationalError:  # colonne déjà présente
                pass
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def _exec(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def _query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    # --- tasks -------------------------------------------------------------
    def upsert_task(self, t: Task) -> None:
        self._exec(
            """INSERT INTO tasks (id,title,due,estimated_minutes,priority,status,source_id,reschedule_count,not_before,boost,project_id,milestone_id)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET title=excluded.title, due=excluded.due,
                 estimated_minutes=excluded.estimated_minutes, priority=excluded.priority,
                 status=excluded.status, source_id=excluded.source_id,
                 reschedule_count=excluded.reschedule_count, not_before=excluded.not_before,
                 boost=excluded.boost, project_id=excluded.project_id,
                 milestone_id=excluded.milestone_id""",
            (t.id, t.title, t.due.isoformat() if t.due else None, t.estimated_minutes,
             t.priority, t.status, t.source_id, t.reschedule_count,
             t.not_before.isoformat() if t.not_before else None, t.boost,
             t.project_id, t.milestone_id),
        )

    @staticmethod
    def _task(r: sqlite3.Row) -> Task:
        return Task(id=r["id"], title=r["title"], due=_dt(r["due"]), not_before=_dt(r["not_before"]), boost=r["boost"] or 0, project_id=r["project_id"], milestone_id=r["milestone_id"],
                    estimated_minutes=r["estimated_minutes"] or 30, priority=r["priority"] or "medium",
                    status=r["status"], source_id=r["source_id"],
                    reschedule_count=r["reschedule_count"] or 0)

    def get_task(self, task_id: str) -> Task | None:
        rows = self._query("SELECT * FROM tasks WHERE id=?", (task_id,))
        return self._task(rows[0]) if rows else None

    def list_tasks(self, *statuses: str) -> list[Task]:
        if statuses:
            q = ",".join("?" * len(statuses))
            rows = self._query(f"SELECT * FROM tasks WHERE status IN ({q}) ORDER BY created_at, id", statuses)
        else:
            rows = self._query("SELECT * FROM tasks ORDER BY created_at, id")
        return [self._task(r) for r in rows]

    def set_task_status(self, task_id: str, status: str) -> None:
        self._exec("UPDATE tasks SET status=? WHERE id=?", (status, task_id))

    def increment_reschedule(self, task_id: str) -> int:
        self._exec("UPDATE tasks SET reschedule_count = reschedule_count + 1 WHERE id=?", (task_id,))
        return self._query("SELECT reschedule_count FROM tasks WHERE id=?", (task_id,))[0][0]

    # --- blocks ------------------------------------------------------------
    def add_block(self, b: Block) -> None:
        self._exec(
            "INSERT INTO blocks (id,task_id,calendar_event_id,start,end,outcome,style) VALUES (?,?,?,?,?,?,?)",
            (b.id, b.task_id, b.calendar_event_id, b.start.isoformat(), b.end.isoformat(), b.outcome, b.style),
        )

    @staticmethod
    def _block(r: sqlite3.Row) -> Block:
        return Block(id=r["id"], task_id=r["task_id"], calendar_event_id=r["calendar_event_id"],
                     start=datetime.fromisoformat(r["start"]), end=datetime.fromisoformat(r["end"]),
                     outcome=r["outcome"], style=r["style"])

    def get_block(self, block_id: str) -> Block | None:
        rows = self._query("SELECT * FROM blocks WHERE id=?", (block_id,))
        return self._block(rows[0]) if rows else None

    def list_blocks(self, task_id: str | None = None, open_only: bool = False) -> list[Block]:
        sql, params = "SELECT * FROM blocks WHERE 1=1", []
        if task_id:
            sql += " AND task_id=?"
            params.append(task_id)
        if open_only:
            sql += " AND outcome IS NULL"
        blocks = [self._block(r) for r in self._query(sql, tuple(params))]
        return sorted(blocks, key=lambda b: b.start)

    def set_block_outcome(self, block_id: str, outcome: str | None) -> None:
        self._exec("UPDATE blocks SET outcome=? WHERE id=?", (outcome, block_id))

    def set_block_event(self, block_id: str, event_id: str | None) -> None:
        self._exec("UPDATE blocks SET calendar_event_id=? WHERE id=?", (event_id, block_id))

    def delete_block(self, block_id: str) -> None:
        self._exec("DELETE FROM blocks WHERE id=?", (block_id,))

    # --- marqueurs « journée entière » des échéances ---------------------------
    def get_marker(self, task_id: str) -> dict | None:
        rows = self._query("SELECT * FROM deadline_markers WHERE task_id=?", (task_id,))
        return dict(rows[0]) if rows else None

    def set_marker(self, task_id: str, event_id: str, due_date: str, title: str) -> None:
        self._exec("INSERT OR REPLACE INTO deadline_markers (task_id,calendar_event_id,due_date,title) VALUES (?,?,?,?)",
                   (task_id, event_id, due_date, title))

    def delete_marker(self, task_id: str) -> None:
        self._exec("DELETE FROM deadline_markers WHERE task_id=?", (task_id,))

    # --- événements (rendez-vous à heure fixe) ---------------------------------
    def add_event_if_new(self, e: Event) -> bool:
        cur = self._exec(
            "INSERT OR IGNORE INTO events (id,title,start,end,location,source_id) VALUES (?,?,?,?,?,?)",
            (e.id, e.title, e.start.isoformat(), e.end.isoformat(), e.location, e.source_id))
        return cur.rowcount == 1

    @staticmethod
    def _event(r: sqlite3.Row) -> Event:
        return Event(id=r["id"], title=r["title"], start=datetime.fromisoformat(r["start"]),
                     end=datetime.fromisoformat(r["end"]), location=r["location"],
                     source_id=r["source_id"], calendar_event_id=r["calendar_event_id"])

    def get_event(self, event_id: str) -> Event | None:
        rows = self._query("SELECT * FROM events WHERE id=?", (event_id,))
        return self._event(rows[0]) if rows else None

    def list_events(self, status: str = "added") -> list[Event]:
        rows = self._query("SELECT * FROM events WHERE status=? ORDER BY start", (status,))
        return [self._event(r) for r in rows]

    def all_events(self) -> list[Event]:
        """Tous les rendez-vous connus, quel que soit leur statut (pour ne jamais en reproposer un doublon)."""
        return [self._event(r) for r in self._query("SELECT * FROM events ORDER BY start")]

    def set_event_status(self, event_id: str, status: str, calendar_event_id: str | None = None) -> None:
        self._exec("UPDATE events SET status=?, calendar_event_id=COALESCE(?, calendar_event_id) WHERE id=?",
                   (status, calendar_event_id, event_id))

    # --- cache d'extraction -------------------------------------------------
    def cache_get(self, key: str) -> list[dict] | None:
        rows = self._query("SELECT payload FROM extraction_cache WHERE key=?", (key,))
        return json.loads(rows[0][0]) if rows else None

    def cache_put(self, key: str, payload: list[dict]) -> None:
        self._exec("INSERT OR REPLACE INTO extraction_cache (key,payload) VALUES (?,?)",
                   (key, json.dumps(payload, ensure_ascii=False)))

    # --- notifications envoyées ----------------------------------------------
    def claim_notification(self, key: str) -> bool:
        """True si la notification n'avait pas encore été envoyée (et la marque comme envoyée)."""
        cur = self._exec("INSERT OR IGNORE INTO notifications (key) VALUES (?)", (key,))
        return cur.rowcount == 1

    def release_notification(self, key: str) -> None:
        self._exec("DELETE FROM notifications WHERE key=?", (key,))

    # --- importance (focus du jour) ---------------------------------------------
    def set_task_boost(self, task_id: str, boost: int) -> None:
        self._exec("UPDATE tasks SET boost=? WHERE id=?", (boost, task_id))

    def set_block_style(self, block_id: str, style: str | None) -> None:
        self._exec("UPDATE blocks SET style=? WHERE id=?", (style, block_id))

    def done_minutes(self) -> dict[str, int]:
        """Minutes de travail terminées, par tâche."""
        out: dict[str, int] = {}
        for b in self.list_blocks():
            if b.outcome == "done":
                out[b.task_id] = out.get(b.task_id, 0) + b.minutes
        return out

    def get_weights(self) -> dict[str, float]:
        return {r["subject"]: r["weight"] for r in self._query("SELECT subject, weight FROM subject_weights")}

    def set_weight(self, subject: str, weight: float) -> None:
        self._exec("INSERT OR REPLACE INTO subject_weights (subject, weight) VALUES (?,?)", (subject, weight))

    def has_notification(self, key: str) -> bool:
        """True si cette notification a déjà été envoyée (sans la marquer)."""
        return bool(self._query("SELECT 1 FROM notifications WHERE key=?", (key,)))

    # --- projets ----------------------------------------------------------------
    @staticmethod
    def _project(r: sqlite3.Row) -> Project:
        return Project(id=r["id"], name=r["name"], category=r["category"], status=r["status"],
                       deadline=date.fromisoformat(r["deadline"]) if r["deadline"] else None,
                       profile=json.loads(r["profile"] or "{}"),
                       created_at=_dt(r["created_at"]) or datetime.now().astimezone(),
                       last_activity=_dt(r["last_activity"]))

    def save_project(self, p: Project) -> None:
        """Crée ou met à jour. Lève `sqlite3.IntegrityError` si la catégorie appartient déjà à un projet non terminé."""
        self._exec(
            """INSERT INTO projects (id,name,category,status,deadline,profile,created_at,last_activity)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET name=excluded.name, category=excluded.category,
                 status=excluded.status, deadline=excluded.deadline, profile=excluded.profile,
                 last_activity=excluded.last_activity""",
            (p.id, p.name, p.category, p.status, p.deadline.isoformat() if p.deadline else None,
             json.dumps(p.profile, ensure_ascii=False), p.created_at.isoformat(),
             p.last_activity.isoformat() if p.last_activity else None))

    def get_project(self, project_id: str) -> Project | None:
        rows = self._query("SELECT * FROM projects WHERE id=?", (project_id,))
        return self._project(rows[0]) if rows else None

    def list_projects(self, *statuses: str) -> list[Project]:
        if statuses:
            q = ",".join("?" * len(statuses))
            rows = self._query(f"SELECT * FROM projects WHERE status IN ({q}) ORDER BY created_at, name", statuses)
        else:
            rows = self._query("SELECT * FROM projects ORDER BY created_at, name")
        return [self._project(r) for r in rows]

    def project_by_category(self, category: str) -> Project | None:
        rows = self._query("SELECT * FROM projects WHERE category=? AND status != 'done'", (category,))
        return self._project(rows[0]) if rows else None

    def touch_project(self, project_id: str, when: datetime) -> None:
        self._exec("UPDATE projects SET last_activity=? WHERE id=?", (when.isoformat(), project_id))

    @staticmethod
    def _milestone(r: sqlite3.Row) -> Milestone:
        return Milestone(id=r["id"], project_id=r["project_id"], title=r["title"], position=r["position"],
                         due=date.fromisoformat(r["due"]) if r["due"] else None, status=r["status"],
                         done_at=_dt(r["done_at"]))

    def save_milestone(self, m: Milestone) -> None:
        self._exec(
            """INSERT INTO milestones (id,project_id,title,position,due,status,done_at) VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET title=excluded.title, position=excluded.position, due=excluded.due,
                 status=excluded.status, done_at=excluded.done_at""",
            (m.id, m.project_id, m.title, m.position, m.due.isoformat() if m.due else None, m.status,
             m.done_at.isoformat() if m.done_at else None))

    def get_milestone(self, milestone_id: str) -> Milestone | None:
        rows = self._query("SELECT * FROM milestones WHERE id=?", (milestone_id,))
        return self._milestone(rows[0]) if rows else None

    def list_milestones(self, project_id: str) -> list[Milestone]:
        rows = self._query("SELECT * FROM milestones WHERE project_id=? ORDER BY position, title", (project_id,))
        return [self._milestone(r) for r in rows]

    def delete_milestone(self, milestone_id: str) -> None:
        """Supprime l'étape ; ses tâches restent dans le projet, sans étape."""
        self._exec("UPDATE tasks SET milestone_id=NULL WHERE milestone_id=?", (milestone_id,))
        self._exec("DELETE FROM milestones WHERE id=?", (milestone_id,))

    def set_task_project(self, task_id: str, project_id: str | None, milestone_id: str | None = None) -> None:
        self._exec("UPDATE tasks SET project_id=?, milestone_id=? WHERE id=?", (project_id, milestone_id, task_id))

    def list_project_tasks(self, project_id: str) -> list[Task]:
        rows = self._query("SELECT * FROM tasks WHERE project_id=? ORDER BY created_at, id", (project_id,))
        return [self._task(r) for r in rows]

    def add_project_example(self, title: str, project_id: str, milestone_id: str | None) -> None:
        self._exec("INSERT INTO project_examples (title,project_id,milestone_id) VALUES (?,?,?)",
                   (title, project_id, milestone_id))

    def recent_examples(self, project_id: str, limit: int = 5) -> list[tuple[str, str | None]]:
        rows = self._query("SELECT title, milestone_id FROM project_examples WHERE project_id=? "
                           "ORDER BY id DESC LIMIT ?", (project_id, limit))
        return [(r["title"], r["milestone_id"]) for r in rows]

    def set_proposal(self, task_id: str, project_id: str | None, milestone_id: str | None,
                     state: str = "proposed") -> None:
        # mise à jour sur place (et non REPLACE) : la ligne garde son rowid, donc sa place dans le lot affiché
        self._exec("INSERT INTO project_proposals (task_id,project_id,milestone_id,state) VALUES (?,?,?,?) "
                   "ON CONFLICT(task_id) DO UPDATE SET project_id=excluded.project_id, "
                   "milestone_id=excluded.milestone_id, state=excluded.state",
                   (task_id, project_id, milestone_id, state))

    def get_proposal(self, task_id: str) -> dict | None:
        rows = self._query("SELECT project_id, milestone_id, state FROM project_proposals WHERE task_id=?",
                           (task_id,))
        return dict(rows[0]) if rows else None

    def get_draft(self) -> dict | None:
        rows = self._query("SELECT * FROM project_draft WHERE id=1")
        if not rows:
            return None
        r = rows[0]
        return {"stage": r["stage"], "profile": json.loads(r["profile"] or "{}"),
                "questions_asked": r["questions_asked"], "pending": r["pending"],
                "updated_at": datetime.fromisoformat(r["updated_at"])}

    def save_draft(self, stage: str, profile: dict, questions_asked: int, pending: str | None,
                   now: datetime) -> None:
        self._exec("INSERT OR REPLACE INTO project_draft (id,stage,profile,questions_asked,pending,updated_at) "
                   "VALUES (1,?,?,?,?,?)",
                   (stage, json.dumps(profile, ensure_ascii=False), questions_asked, pending, now.isoformat()))

    def clear_draft(self) -> None:
        self._exec("DELETE FROM project_draft WHERE id=1")

    # --- suggestions de tâches (assistant de projet) -------------------------------------
    @staticmethod
    def _suggestion(r: sqlite3.Row) -> Suggestion:
        return Suggestion(id=r["id"], batch=r["batch"], project_id=r["project_id"], milestone_id=r["milestone_id"],
                          title=r["title"], estimated_minutes=r["estimated_minutes"], state=r["state"],
                          created_at=datetime.fromisoformat(r["created_at"]))

    def save_suggestions(self, items: list[Suggestion]) -> None:
        for s in items:
            self._exec("INSERT INTO project_suggestions (id,batch,project_id,milestone_id,title,estimated_minutes,"
                       "state,created_at) VALUES (?,?,?,?,?,?,?,?)",
                       (s.id, s.batch, s.project_id, s.milestone_id, s.title, s.estimated_minutes, s.state,
                        s.created_at.isoformat()))

    def get_suggestion(self, suggestion_id: str) -> Suggestion | None:
        rows = self._query("SELECT * FROM project_suggestions WHERE id=?", (suggestion_id,))
        return self._suggestion(rows[0]) if rows else None

    def list_batch(self, batch: str) -> list[Suggestion]:
        rows = self._query("SELECT * FROM project_suggestions WHERE batch=? ORDER BY rowid", (batch,))
        return [self._suggestion(r) for r in rows]

    def list_project_suggestions(self, project_id: str, limit: int | None = None) -> list[Suggestion]:
        """Suggestions d'un projet, les plus récentes d'abord."""
        sql, params = "SELECT * FROM project_suggestions WHERE project_id=? ORDER BY rowid DESC", [project_id]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        return [self._suggestion(r) for r in self._query(sql, tuple(params))]

    def set_suggestion_state(self, suggestion_id: str, state: str) -> None:
        self._exec("UPDATE project_suggestions SET state=? WHERE id=?", (state, suggestion_id))

    def pending_batch(self, project_id: str) -> str | None:
        """Lot le plus récent du projet qui a encore une suggestion à traiter."""
        rows = self._query("SELECT batch FROM project_suggestions WHERE project_id=? AND state='proposed' "
                           "ORDER BY rowid DESC LIMIT 1", (project_id,))
        return rows[0]["batch"] if rows else None

    def dismiss_stale_suggestions(self, cutoff: datetime) -> int:
        """Écarte les suggestions encore `proposed` créées avant `cutoff`. Renvoie leur nombre."""
        stale = [r["id"] for r in self._query("SELECT id, created_at FROM project_suggestions WHERE state='proposed'")
                 if datetime.fromisoformat(r["created_at"]) < cutoff]
        for sid in stale:
            self.set_suggestion_state(sid, "dismissed")
        return len(stale)

    def delete_batch(self, batch: str) -> None:
        """Oublie les suggestions encore `proposed` d'un lot (jamais vu par l'utilisateur : message non parti)."""
        self._exec("DELETE FROM project_suggestions WHERE batch=? AND state='proposed'", (batch,))

    # --- propositions de rangement en lot --------------------------------------------------
    def list_lot_proposals(self, project_id: str) -> list[dict]:
        """Propositions d'étape d'un lot (`lot`, `lot_ok`, `lot_no`) pour les tâches du projet, dans l'ordre."""
        rows = self._query("SELECT pp.task_id, pp.milestone_id, pp.state FROM project_proposals pp "
                           "JOIN tasks t ON t.id = pp.task_id WHERE t.project_id=? AND pp.state LIKE 'lot%' "
                           "ORDER BY pp.rowid", (project_id,))
        return [dict(r) for r in rows]

    def clear_lot_proposals(self, project_id: str) -> None:
        self._exec("DELETE FROM project_proposals WHERE state LIKE 'lot%' AND task_id IN "
                   "(SELECT id FROM tasks WHERE project_id=?)", (project_id,))
