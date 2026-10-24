"""Modèles Pydantic du coach."""
from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

Priority = Literal["low", "medium", "high"]
TaskStatus = Literal["pending", "scheduled", "done", "dropped"]
Outcome = Literal["done", "not_finished", "rescheduled", "missed"]


def new_id() -> str:
    return uuid.uuid4().hex


def short_id() -> str:
    """Identifiant court (8 caractères hexadécimaux) des projets et des étapes : tient dans un `callback_data`."""
    return uuid.uuid4().hex[:8]


ProjectStatus = Literal["active", "paused", "done"]
MilestoneStatus = Literal["open", "done"]


class Task(BaseModel):
    id: str = Field(default_factory=new_id)
    title: str = Field(min_length=1)
    due: datetime | None = None
    # tâche à faire un jour précis (« prendre les chaussures vendredi ») : jamais placée avant
    not_before: datetime | None = None
    # ajustement d'importance posé par l'utilisateur (boutons ⬆ / ⬇) : de -1 à +2
    boost: int = Field(default=0, ge=-1, le=2)
    project_id: str | None = None
    milestone_id: str | None = None
    estimated_minutes: int = Field(default=30, gt=0)
    priority: Priority = "medium"
    status: TaskStatus = "pending"
    source_id: str | None = None
    reschedule_count: int = Field(default=0, ge=0)


class Event(BaseModel):
    """Rendez-vous / événement à date ou lieu précis : va dans l'agenda tel quel, jamais replacé."""
    id: str = Field(default_factory=new_id)
    title: str = Field(min_length=1)
    start: datetime
    end: datetime
    location: str | None = None
    source_id: str | None = None
    calendar_event_id: str | None = None

    @model_validator(mode="after")
    def _check_order(self) -> "Event":
        if self.end <= self.start:
            raise ValueError("end must be after start")
        return self


class Block(BaseModel):
    id: str = Field(default_factory=new_id)
    task_id: str
    calendar_event_id: str | None = None
    start: datetime
    end: datetime
    outcome: Outcome | None = None
    style: str | None = None  # dernier style appliqué dans l'agenda : focus | haut | moyen | faible

    @model_validator(mode="after")
    def _check_order(self) -> "Block":
        if self.end <= self.start:
            raise ValueError("end must be after start")
        return self

    @property
    def minutes(self) -> int:
        return int((self.end - self.start).total_seconds() // 60)


class Project(BaseModel):
    id: str = Field(default_factory=short_id)
    name: str = Field(min_length=1)
    category: str | None = None  # code court du préfixe de titre (`IA`, `Stg`…)
    status: ProjectStatus = "active"
    deadline: date | None = None
    profile: dict[str, Any] = Field(default_factory=dict)  # objectif, limites, livrables, équipe, temps par semaine
    created_at: datetime = Field(default_factory=lambda: datetime.now().astimezone())
    last_activity: datetime | None = None


class Milestone(BaseModel):
    id: str = Field(default_factory=short_id)
    project_id: str
    title: str = Field(min_length=1)
    position: int = 0
    due: date | None = None
    status: MilestoneStatus = "open"
    done_at: datetime | None = None


SuggestionState = Literal["proposed", "added", "planned", "dismissed"]


class Suggestion(BaseModel):
    """Tâche proposée par l'assistant de projet ; devient une vraie tâche seulement après un clic."""
    id: str = Field(default_factory=short_id)
    batch: str                       # un lot = un message de suggestions
    project_id: str
    milestone_id: str | None = None
    title: str = Field(min_length=1)
    estimated_minutes: int = Field(default=30, gt=0)
    state: SuggestionState = "proposed"
    created_at: datetime = Field(default_factory=lambda: datetime.now().astimezone())
