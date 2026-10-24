"""Accès agenda : Protocol + implémentation Google Calendar + fake pour les tests."""
from __future__ import annotations

import functools
import hashlib
import logging
import os
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol, runtime_checkable
from zoneinfo import ZoneInfo

from coach.courses import clean_title
from coach.models import Block, Event, Task

TZ_NAME = "Europe/Paris"
TZ = ZoneInfo(TZ_NAME)
TITLE_PREFIX = "🎯 "
LEGACY_TITLE_PREFIX = "[Focus] "  # anciens événements déjà dans l'agenda
STAR = "⭐ "
# importance -> couleur Google (colorId) : orange pour le focus, échelle de bleus (foncé = plus important) sinon
STYLE_COLOR = {"focus": "6", "haut": "9", "moyen": "7", "faible": "1"}
TASK_ID_RE = re.compile(r"task_id:\s*(\S+)")

Interval = tuple[datetime, datetime]
FREEBUSY_CHUNK_DAYS = 60

# couleurs Google Calendar (colorId 1-11) par préfixe de matière `[Alg] ...`
COLORS = {"MP": "9", "EPS": "6", "Alg": "5", "BD": "7", "LV": "10", "GL": "8",
          "Stg": "4", "C": "11", "Out": "3", "Arc": "1", "IA": "2", "PPP": "6"}


def color_id(title: str) -> str | None:
    """Couleur stable selon le préfixe `[matière]` du titre ; None si pas de préfixe."""
    m = re.match(r"\[([^\]]+)\]", title)
    if not m:
        return None
    key = m.group(1)
    return COLORS.get(key) or str(1 + sum(map(ord, key)) % 11)


def stable_id(*parts: str) -> str:
    """Identifiant Google Calendar calculé (hex = alphabet autorisé) : créer deux fois le même événement,
    même depuis un autre ordinateur ou après une base réinitialisée, ne peut plus produire de doublon."""
    return hashlib.sha1("|".join(parts).encode()).hexdigest()


def marker_id(task: Task) -> str:
    return stable_id("marker", task.id)


def block_id(block: Block, task: Task) -> str:
    return stable_id("block", task.id, block.start.astimezone(TZ).isoformat())


def fixed_event_id(event: Event) -> str:
    return stable_id("event", event.id)


def marker_title(task: Task) -> str:
    return f"📌 {task.title}"


def marker_body(task: Task) -> dict:
    d = (task.due or datetime.now(TZ)).astimezone(TZ).date()
    body: dict[str, Any] = {
        "summary": marker_title(task), "description": f"task_id: {task.id}\nmarqueur d'échéance",
        "start": {"date": d.isoformat()}, "end": {"date": (d + timedelta(days=1)).isoformat()},
        "transparency": "transparent",  # n'occupe pas l'agenda : le planificateur ignore ces marqueurs
    }
    if c := color_id(task.title):
        body["colorId"] = c
    return body


def event_title(task: Task, level: str | None = None) -> str:
    """`🎯 titre`, avec une étoile pour les tâches du focus (le préfixe `🎯 ` reste en tête : `agenda()` s'en sert)."""
    return f"{TITLE_PREFIX}{STAR if level == 'focus' else ''}{task.title}"


def event_description(task: Task, block: Block) -> str:
    return f"task_id: {task.id}\nblock_id: {block.id}"


@runtime_checkable
class CalendarClient(Protocol):
    def freebusy(self, start: datetime, end: datetime) -> list[Interval]: ...
    def create_event(self, block: Block, task: Task, level: str | None = None) -> str: ...
    def recolor_event(self, event_id: str, title: str, color_id: str) -> None: ...
    def create_fixed_event(self, event: Event) -> str: ...
    def create_deadline_marker(self, task: Task) -> str: ...
    def update_deadline_marker(self, event_id: str, task: Task) -> None: ...
    def agenda(self, start: datetime, end: datetime) -> list[tuple[datetime, datetime, str]]: ...
    def move_event(self, event_id: str, start: datetime, end: datetime) -> None: ...
    def delete_event(self, event_id: str) -> None: ...


DEFAULT_SCOPES = (
    "https://www.googleapis.com/auth/gmail.readonly,https://www.googleapis.com/auth/calendar.events,"
    "https://www.googleapis.com/auth/calendar.readonly,https://www.googleapis.com/auth/drive,"
    "https://www.googleapis.com/auth/documents"
)


def credentials_from_env(token_file: str = "./secrets/token.json") -> Any:  # pragma: no cover
    """OAuth Google (Gmail + Calendar partagent le même token). Ouvre le navigateur au 1er lancement."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    scopes = os.environ.get("GOOGLE_SCOPES", DEFAULT_SCOPES).split(",")
    creds = None
    if os.path.exists(token_file):
        # on renouvelle avec les droits DÉJÀ accordés au jeton : demander un droit non accordé fait
        # échouer le renouvellement (invalid_scope) et casserait Gmail, Calendar et Drive d'un coup
        creds = Credentials.from_authorized_user_file(token_file)
        missing = set(scopes) - set(creds.scopes or [])
        if missing:
            logging.getLogger("google_auth").warning(
                "droits Google non accordés (refaire l'authentification : python -m scripts.reauth_google) : %s",
                ", ".join(sorted(m.rsplit("/", 1)[-1] for m in missing)))
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                os.environ.get("GOOGLE_CLIENT_SECRET_FILE", "./secrets/client_secret.json"), scopes)
            creds = flow.run_local_server(port=0)
        with open(token_file, "w", encoding="utf-8") as f:
            f.write(creds.to_json())
    return creds


class LazyGoogleService:
    """Ressource Google construite au premier usage, puis réessayée tant que le jeton est refusé.

    Un jeton expiré ou révoqué ne doit pas empêcher le bot de démarrer (Telegram, Notion, IA marchent sans
    Google) : l'erreur remonte au job qui s'en sert (signalé par `health.guard`), et le prochain essai
    reprend dès que `secrets/token.json` est remplacé, sans redémarrage.
    """

    def __init__(self, api: str, version: str, token_file: str = "./secrets/token.json") -> None:
        self._args = (api, version, token_file)
        self._svc: Any = None
        self._lock = threading.Lock()

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        with self._lock:
            if self._svc is None:
                from googleapiclient.discovery import build
                api, version, token_file = self._args
                self._svc = build(api, version, credentials=credentials_from_env(token_file), cache_discovery=False)
        return getattr(self._svc, name)


class GoogleCalendarClient:
    """Implémentation Google Calendar API v3.

    `service` = ressource retournée par `googleapiclient.discovery.build("calendar","v3",...)`.
    Ne fait aucun accès réseau à la construction ; voir `from_env` pour l'OAuth.
    """

    def __init__(self, service: Any, calendar_id: str = "primary") -> None:
        self._svc = service
        self._cal = calendar_id
        # le client HTTP de Google n'est pas thread-safe : bot, scheduler et jobs partagent ce service
        self._lock = threading.RLock()

    @classmethod
    def from_env(cls, token_file: str = "./secrets/token.json") -> "GoogleCalendarClient":  # pragma: no cover
        return cls(LazyGoogleService("calendar", "v3", token_file), os.environ.get("GOOGLE_CALENDAR_ID", "primary"))

    def _calendar_ids(self) -> list[str]:
        """Calendrier principal + calendriers abonnés (ex. iCal de l'emploi du temps INSA)."""
        try:
            items = self._svc.calendarList().list().execute().get("items", [])
            ids = [c["id"] for c in items if c.get("selected", True)]
        except Exception:  # scope insuffisant / service factice : on se limite au calendrier principal
            ids = []
        return list(dict.fromkeys([self._cal, *ids]))

    def freebusy(self, start: datetime, end: datetime) -> list[Interval]:
        """Google refuse les plages trop longues (« timeRangeTooLong ») : on découpe par tranches de 60 jours."""
        out: list[Interval] = []
        cur = start
        while cur < end:
            nxt = min(cur + timedelta(days=FREEBUSY_CHUNK_DAYS), end)
            out += self._freebusy_once(cur, nxt)
            cur = nxt
        return out

    def _freebusy_once(self, start: datetime, end: datetime) -> list[Interval]:
        ids = self._calendar_ids()
        body = {
            "timeMin": start.astimezone(TZ).isoformat(),
            "timeMax": end.astimezone(TZ).isoformat(),
            "timeZone": TZ_NAME,
            "items": [{"id": i} for i in ids],
        }
        resp = self._svc.freebusy().query(body=body).execute()
        busy = [b for i in ids for b in resp.get("calendars", {}).get(i, {}).get("busy", [])]
        return [
            (
                datetime.fromisoformat(b["start"].replace("Z", "+00:00")).astimezone(TZ),
                datetime.fromisoformat(b["end"].replace("Z", "+00:00")).astimezone(TZ),
            )
            for b in busy
        ]

    def create_event(self, block: Block, task: Task, level: str | None = None) -> str:
        body = {
            "summary": event_title(task, level),
            "description": event_description(task, block),
            "start": {"dateTime": block.start.astimezone(TZ).isoformat(), "timeZone": TZ_NAME},
            "end": {"dateTime": block.end.astimezone(TZ).isoformat(), "timeZone": TZ_NAME},
        }
        if level in STYLE_COLOR:
            body["colorId"] = STYLE_COLOR[level]
        return self._insert(body, block_id(block, task))

    def _insert(self, body: dict, event_id: str) -> str:
        """Insère avec un identifiant calculé : si l'événement existe déjà (409), on le réutilise (et on le
        rétablit s'il avait été supprimé) au lieu d'en créer un second."""
        try:
            return str(self._svc.events().insert(calendarId=self._cal, body={**body, "id": event_id}).execute()["id"])
        except Exception as exc:  # noqa: BLE001 - HttpError (googleapiclient importé paresseusement)
            status = getattr(getattr(exc, "resp", None), "status", None) or getattr(exc, "status_code", None)
            if int(status or 0) != 409:
                raise
            self._svc.events().patch(calendarId=self._cal, eventId=event_id,
                                     body={**body, "status": "confirmed"}).execute()
            return event_id

    def create_deadline_marker(self, task: Task) -> str:
        return self._insert(marker_body(task), marker_id(task))

    def update_deadline_marker(self, event_id: str, task: Task) -> None:
        self._svc.events().patch(calendarId=self._cal, eventId=event_id, body=marker_body(task)).execute()

    def agenda(self, start: datetime, end: datetime) -> list[tuple[datetime, datetime, str]]:
        """Événements des autres calendriers (cours, sport) avec leur titre ; exclut ce que le coach a créé."""
        out: list[tuple[datetime, datetime, str]] = []
        for cal in self._calendar_ids():
            try:
                items = self._svc.events().list(
                    calendarId=cal, timeMin=start.astimezone(TZ).isoformat(), timeMax=end.astimezone(TZ).isoformat(),
                    singleEvents=True, orderBy="startTime",
                    fields="items(summary,description,status,transparency,start,end)").execute().get("items", [])
            except Exception:
                continue
            for it in items:
                if it.get("status") == "cancelled" or it.get("transparency") == "transparent":
                    continue
                if "dateTime" not in it.get("start", {}):  # journée entière
                    continue
                title = it.get("summary", "(sans titre)")
                if title.startswith((TITLE_PREFIX, LEGACY_TITLE_PREFIX)) or "event_id:" in (it.get("description") or ""):
                    continue
                title = clean_title(title)  # codes ADE -> « POO1 · TD »
                out.append((datetime.fromisoformat(it["start"]["dateTime"]).astimezone(TZ),
                            datetime.fromisoformat(it["end"]["dateTime"]).astimezone(TZ), title))
        return sorted(set(out))

    def create_fixed_event(self, event: Event) -> str:
        body: dict[str, Any] = {
            "summary": event.title,
            "description": f"event_id: {event.id}",
            "start": {"dateTime": event.start.astimezone(TZ).isoformat(), "timeZone": TZ_NAME},
            "end": {"dateTime": event.end.astimezone(TZ).isoformat(), "timeZone": TZ_NAME},
        }
        if event.location:
            body["location"] = event.location
        return self._insert(body, fixed_event_id(event))

    def move_event(self, event_id: str, start: datetime, end: datetime) -> None:
        body = {
            "start": {"dateTime": start.astimezone(TZ).isoformat(), "timeZone": TZ_NAME},
            "end": {"dateTime": end.astimezone(TZ).isoformat(), "timeZone": TZ_NAME},
        }
        self._svc.events().patch(calendarId=self._cal, eventId=event_id, body=body).execute()

    def recolor_event(self, event_id: str, title: str, color_id: str) -> None:
        self._svc.events().patch(calendarId=self._cal, eventId=event_id,
                                 body={"summary": title, "colorId": color_id}).execute()

    def delete_event(self, event_id: str) -> None:
        self._svc.events().delete(calendarId=self._cal, eventId=event_id).execute()


def _locked(fn: Any) -> Any:
    @functools.wraps(fn)
    def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
        with self._lock:
            return fn(self, *args, **kwargs)
    return wrapper


for _name in ("freebusy", "create_event", "create_fixed_event", "create_deadline_marker",
              "update_deadline_marker", "agenda", "move_event", "delete_event", "recolor_event"):
    setattr(GoogleCalendarClient, _name, _locked(getattr(GoogleCalendarClient, _name)))


@dataclass
class FakeEvent:
    id: str
    summary: str
    description: str
    start: datetime
    end: datetime
    color: str | None = None


class FakeCalendarClient:
    """Agenda en mémoire (tests). `external_busy` = cours / entraînements."""

    def __init__(self, external_busy: list[Interval] | None = None) -> None:
        self.external_busy: list[Interval] = list(external_busy or [])
        self.events: dict[str, FakeEvent] = {}
        self.markers: dict[str, dict] = {}
        self._n = 0

    def freebusy(self, start: datetime, end: datetime) -> list[Interval]:
        own = [(e.start, e.end) for e in self.events.values()]
        return [(s, e) for s, e in self.external_busy + own if e > start and s < end]

    def create_event(self, block: Block, task: Task, level: str | None = None) -> str:
        eid = block_id(block, task)
        self.events[eid] = FakeEvent(eid, event_title(task, level), event_description(task, block),
                                     block.start, block.end, STYLE_COLOR.get(level or ""))
        return eid

    def recolor_event(self, event_id: str, title: str, color_id: str) -> None:
        ev = self.events[event_id]
        ev.summary, ev.color = title, color_id

    def agenda(self, start: datetime, end: datetime) -> list[tuple[datetime, datetime, str]]:
        return [(s, e, "") for s, e in self.external_busy if e > start and s < end]

    def create_deadline_marker(self, task: Task) -> str:
        eid = marker_id(task)
        self.markers[eid] = marker_body(task)
        return eid

    def update_deadline_marker(self, event_id: str, task: Task) -> None:
        self.markers[event_id] = marker_body(task)

    def create_fixed_event(self, event: Event) -> str:
        eid = fixed_event_id(event)
        self.events[eid] = FakeEvent(eid, event.title, f"event_id: {event.id}", event.start, event.end)
        return eid

    def move_event(self, event_id: str, start: datetime, end: datetime) -> None:
        ev = self.events[event_id]
        ev.start, ev.end = start, end

    def delete_event(self, event_id: str) -> None:
        self.events.pop(event_id, None)
        self.markers.pop(event_id, None)
