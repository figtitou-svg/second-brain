"""Extraction de tâches via le LLM (JSON validé Pydantic, 1 retry, cache par hash)."""
from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any, Callable, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, ValidationError, field_validator

from coach.models import Event, Priority, Task

log = logging.getLogger("coach.extract")

Sensitivity = Literal["private", "public"]
TZ = ZoneInfo("Europe/Paris")
MIN_CONFIDENCE = 0.6
# durées proposées au LLM, puis imposées en code : le planificateur reçoit toujours une valeur propre
DURATION_SCALE = (5, 10, 15, 20, 30, 45, 60, 90, 120, 180, 240, 360, 480)

DURATION_RULES = (
    "estimated_minutes = le temps de TRAVAIL RÉEL pour une personne normale (pas le délai écoulé), préparation et "
    "trajets compris, choisi parmi : " + ", ".join(str(m) for m in DURATION_SCALE) + ". "
    "Repères : appeler / écrire un message court 10 ; prendre ou acheter un objet 15 ; envoyer un document déjà prêt 10 ; "
    "formulaire d'une page dont tu connais déjà tout 30 ; tout autre formulaire ou dossier à remplir (pièces ou "
    "informations à retrouver, stage, visa, administration, assurance) au moins 60, et 90 s'il vient d'un stage ou "
    "d'un séjour à l'étranger ; "
    "lettre de motivation ou CV 120 ; révision d'un chapitre 90 ; rapport ou dossier 240 ; projet entier 480. "
    "En hésitation entre deux valeurs : prends la plus grande. "
)

SYSTEM_PROMPT = (
    "Tu extrais d'un texte (1) les TÂCHES à faire par l'utilisateur, sans heure imposée, et (2) les "
    "ÉVÉNEMENTS : rendez-vous, cours, réunions, matchs, soirées... qui ont une date/heure ou un lieu précis. "
    "Un événement n'est JAMAIS une tâche : ne le mets pas dans tasks. "
    "Ne crée PAS de tâche pour remplir/compléter ses informations ou son profil, ni pour répondre, accepter "
    "ou décliner une invitation. Un texte donne en général UNE seule tâche : ne découpe pas en sous-étapes, "
    "ne répète pas la même tâche sous deux formulations, et mets une liste vide si rien n'est vraiment à faire. "
    "Réponds UNIQUEMENT avec un JSON de la forme "
    '{"tasks":[{"title":str,"do_on":"AAAA-MM-JJ"|null,"due":ISO8601|null,"estimated_minutes":int,'
    '"priority":"low"|"medium"|"high","confidence":0..1}],'
    '"events":[{"title":str,"start":ISO8601,"end":ISO8601|null,"location":str|null,"confidence":0..1}]}. '
    "Mets end à null si la durée n'est pas donnée. Listes vides si rien. "
    "Fuseau Europe/Paris. Aucun texte hors JSON. "
    "Pour les dates, utilise le calendrier fourni (ne calcule pas les jours de la semaine). Distingue bien : "
    "(a) une tâche à faire UN JOUR PRÉCIS, sans heure (« prendre les chaussures de sécu vendredi », « appeler la "
    "banque demain ») -> do_on = ce jour, due = null ; "
    "(b) une ÉCHÉANCE (« avant vendredi », « pour vendredi », « d'ici vendredi », « à rendre vendredi ») -> "
    "do_on = null, due = ce vendredi à 23:59:59, jamais la veille. Sans date : do_on = null et due = null. "
    + DURATION_RULES
)

PROJECT_RULES = (
    " Si une tâche appartient clairement à l'un des projets actifs listés, ajoute à cette tâche "
    '"project" (le nom EXACT du projet) et "milestone" (le titre EXACT d\'une de ses étapes ouvertes, ou null) ; '
    "sinon project = null et milestone = null."
)

LLMFn = Callable[..., str]


class ExtractedTask(BaseModel):
    title: str = Field(min_length=1)
    do_on: date | None = None  # jour précis où la faire (sinon : due = échéance, faisable dès maintenant)
    due: datetime | None = None
    estimated_minutes: int = Field(default=30, gt=0)
    priority: Priority = "medium"
    confidence: float = Field(default=1.0, ge=0, le=1)
    project: str | None = None    # nom d'un projet actif, si la tâche lui appartient clairement
    milestone: str | None = None  # titre d'une de ses étapes ouvertes

    @field_validator("project", "milestone", mode="before")
    @classmethod
    def _hint_text(cls, v: Any) -> str | None:
        """Un indice de rattachement mal formé (False, 0, {}…) est ignoré : il ne doit pas faire perdre la tâche."""
        return (v.strip() or None) if isinstance(v, str) else None

    @field_validator("estimated_minutes")
    @classmethod
    def _snap(cls, v: int) -> int:
        """Ramène la durée à la valeur la plus proche de l'échelle (5 min à 8 h) ; à égalité, la plus grande."""
        return min(DURATION_SCALE, key=lambda m: (abs(m - v), -m))


class ExtractedEvent(BaseModel):
    title: str = Field(min_length=1)
    start: datetime
    end: datetime | None = None
    location: str | None = None
    confidence: float = Field(default=1.0, ge=0, le=1)


class ExtractionResponse(BaseModel):
    tasks: list[ExtractedTask] = Field(default_factory=list)
    events: list[ExtractedEvent] = Field(default_factory=list)


@dataclass
class Extraction:
    tasks: list[Task]
    events: list[Event]
    # id de tâche -> (nom de projet, titre d'étape) proposés par l'IA ; vide sans projets
    hints: dict[str, tuple[str | None, str | None]] = field(default_factory=dict)

DEFAULT_EVENT_MINUTES = 60


class ExtractCache:
    """Cache en mémoire, ou adossé à `coach.db.Database` si fourni."""

    def __init__(self, db: Any | None = None) -> None:
        self._db = db
        self._mem: dict[str, list[dict]] = {}

    def get(self, key: str) -> list[dict] | None:
        return self._db.cache_get(key) if self._db else self._mem.get(key)

    def put(self, key: str, payload: list[dict]) -> None:
        if self._db:
            self._db.cache_put(key, payload)
        else:
            self._mem[key] = payload


_default_cache = ExtractCache()


def configure_cache(cache: ExtractCache) -> None:
    global _default_cache
    _default_cache = cache


def content_hash(text: str, sensitivity: str) -> str:
    return hashlib.sha256(f"{sensitivity}\n{text}".encode()).hexdigest()


def _resolve_llm() -> LLMFn:
    try:  # import tolérant : le module est écrit en parallèle par l'agent ROUTER
        from llm_router.router import complete  # type: ignore[import-not-found]
    except ImportError as e:  # pragma: no cover - dépend de l'état du repo
        raise RuntimeError("llm_router.router.complete indisponible") from e
    return complete


def _json_from(raw: str) -> Any:
    s = raw.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", s, re.S)
    if m:
        s = m.group(1).strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        a, b = s.find("{"), s.rfind("}")
        if a != -1 and b > a:
            return json.loads(s[a : b + 1])
        raise


def _parse(raw: str) -> ExtractionResponse:
    return ExtractionResponse.model_validate(_json_from(raw))


def _task_id(source_id: str, title: str) -> str:
    return hashlib.sha1(f"{source_id}|{title.strip().lower()}".encode()).hexdigest()[:16]


def _build(items: list[ExtractedTask], source_id: str, now: datetime) -> list[Task]:
    tasks: list[Task] = []
    for it in items:
        if it.confidence < MIN_CONFIDENCE:
            continue
        due, not_before = it.due, None
        if it.do_on is not None:  # ce jour-là : ni avant, ni après
            not_before = datetime.combine(it.do_on, time(0, 0), tzinfo=TZ)
            due = datetime.combine(it.do_on, time(23, 59, 59), tzinfo=TZ)
        elif due is not None and due.tzinfo is None:  # sans date : pas d'échéance inventée, placée au plus tôt
            due = due.replace(tzinfo=TZ)
        tasks.append(Task(
            id=_task_id(source_id, it.title), title=it.title.strip(), due=due, not_before=not_before,
            estimated_minutes=it.estimated_minutes, priority=it.priority, source_id=source_id,
        ))
    return tasks


def _build_events(items: list[ExtractedEvent], source_id: str) -> list[Event]:
    events: list[Event] = []
    for it in items:
        if it.confidence < MIN_CONFIDENCE:
            continue
        start = it.start if it.start.tzinfo else it.start.replace(tzinfo=TZ)
        end = it.end or start + timedelta(minutes=DEFAULT_EVENT_MINUTES)
        if end.tzinfo is None:
            end = end.replace(tzinfo=TZ)
        if end <= start:
            end = start + timedelta(minutes=DEFAULT_EVENT_MINUTES)
        eid = hashlib.sha1(f"{source_id}|{it.title.strip().lower()}|{start.isoformat()}".encode()).hexdigest()[:16]
        events.append(Event(id=eid, title=it.title.strip(), start=start, end=end,
                            location=(it.location or None), source_id=source_id))
    return events


def _projects_block(projects: list[dict]) -> str:
    lines = ["Projets actifs de l'utilisateur :"]
    for p in projects:
        line = f"- {p['name']}" + (f" (catégorie {p['category']})" if p.get("category") else "")
        if p.get("milestones"):
            line += " ; étapes ouvertes : " + ", ".join(p["milestones"])
        examples = p.get("examples") or []
        if examples:
            line += " ; exemples confirmés : " + " | ".join(
                f"« {t} »" + (f" → {m}" if m else "") for t, m in examples)
        lines.append(line)
    return "\n".join(lines)


def _hints(items: list[ExtractedTask], source_id: str) -> dict[str, tuple[str | None, str | None]]:
    return {_task_id(source_id, it.title): (it.project, it.milestone)
            for it in items if it.project and it.confidence >= MIN_CONFIDENCE}


def extract_tasks(text: str, source_id: str, sensitivity: Sensitivity, **kw: Any) -> list[Task]:
    """Compatibilité : uniquement les tâches (voir `extract_items` pour tâches + événements)."""
    return extract_items(text, source_id, sensitivity, **kw).tasks


def extract_items(
    text: str,
    source_id: str,
    sensitivity: Sensitivity,
    *,
    llm: LLMFn | None = None,
    cache: ExtractCache | None = None,
    now: datetime | None = None,
    projects: list[dict] | None = None,
) -> Extraction:
    """Extrait tâches et événements de `text`. Vide si l'extraction échoue deux fois.

    Les exceptions du LLM (dont SensitivityError) ne sont pas avalées.
    """
    cache = cache or _default_cache
    now = now or datetime.now(TZ)
    key = content_hash(text, sensitivity)
    if projects:  # la liste des projets fait partie de la clé : un rattachement ancien ne doit pas être resservi
        key += ":p" + hashlib.sha256(json.dumps(projects, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:12]

    cached = cache.get(key)
    if cached is not None:
        items = [ExtractedTask.model_validate(d) for d in cached]
        ev_raw = cache.get(key + ":ev") or []
        return Extraction(_build(items, source_id, now),
                          _build_events([ExtractedEvent.model_validate(d) for d in ev_raw], source_id),
                          _hints(items, source_id) if projects else {})

    complete = llm or _resolve_llm()
    jours = ("lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche")
    # calendrier explicite : les modèles se trompent d'un jour quand ils calculent « vendredi » ou « mardi prochain »
    calendrier = "\n".join(f"{jours[d.weekday()]} {d.date().isoformat()}"
                           for d in (now + timedelta(days=i) for i in range(15)))
    prompt = (f"Date actuelle : {jours[now.weekday()]} {now.isoformat()}\n\n"
              f"Calendrier des prochains jours :\n{calendrier}\n\nTexte :\n{text}")
    system = SYSTEM_PROMPT
    if projects:
        prompt = f"{_projects_block(projects)}\n\n{prompt}"
        system = SYSTEM_PROMPT + PROJECT_RULES
    response: ExtractionResponse | None = None
    last_err: Exception | None = None
    for attempt in range(2):  # 1 essai + 1 seul retry
        p = prompt if attempt == 0 else (
            f"{prompt}\n\nTa réponse précédente était invalide ({last_err}). "
            "Réponds uniquement avec le JSON demandé."
        )
        raw = complete(p, sensitivity=sensitivity, system=system, json_mode=True)
        try:
            response = _parse(raw)
            break
        except (ValueError, ValidationError) as e:  # JSONDecodeError est un ValueError
            last_err = e
            log.warning("extraction invalide (essai %d) pour %s : %s", attempt + 1, source_id, e)
    if response is None:
        log.error("extraction abandonnée pour %s : %s", source_id, last_err)
        return Extraction([], [])

    cache.put(key, [t.model_dump(mode="json") for t in response.tasks])
    cache.put(key + ":ev", [e.model_dump(mode="json") for e in response.events])
    return Extraction(_build(response.tasks, source_id, now), _build_events(response.events, source_id),
                      _hints(response.tasks, source_id) if projects else {})
