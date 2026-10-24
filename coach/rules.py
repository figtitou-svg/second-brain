"""Chargeur typé de rules.yaml."""
from __future__ import annotations

from datetime import date, time
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
DEFAULT_RULES_PATH = Path(__file__).with_name("rules.yaml")


def _parse_hhmm(v: object) -> time:
    if isinstance(v, time):
        return v
    if isinstance(v, int):  # YAML sexagésimal piège : 08:00 -> 480
        return time(v // 60, v % 60)
    h, m = str(v).split(":")
    return time(int(h), int(m))


class HoursRange(BaseModel):
    start: time
    end: time

    @model_validator(mode="before")
    @classmethod
    def _from_pair(cls, v: object) -> object:
        if isinstance(v, (list, tuple)):
            if len(v) != 2:
                raise ValueError("expected [start, end]")
            return {"start": _parse_hhmm(v[0]), "end": _parse_hhmm(v[1])}
        return v

    @model_validator(mode="after")
    def _check(self) -> "HoursRange":
        if self.end <= self.start:
            raise ValueError("end must be after start")
        return self


class BlockedSlot(BaseModel):
    day: Literal[
        "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"
    ]
    from_: time = Field(alias="from")
    to: time
    label: str = ""

    model_config = {"populate_by_name": True}

    @field_validator("day", mode="before")
    @classmethod
    def _lower(cls, v: object) -> object:
        return v.lower() if isinstance(v, str) else v

    @field_validator("from_", "to", mode="before")
    @classmethod
    def _t(cls, v: object) -> time:
        return _parse_hhmm(v)


class AllowedHours(BaseModel):
    weekday: HoursRange
    weekend: HoursRange


class LeadTier(BaseModel):
    """Palier du délai de démarrage : jusqu'à `up_to_minutes` de durée estimée (sans limite si absent)."""
    up_to_minutes: int | None = Field(default=None, gt=0)
    days: int = Field(ge=0)


class Rules(BaseModel):
    timezone: str = "Europe/Paris"
    allowed_hours: AllowedHours
    reminder_hours: AllowedHours | None = None  # plage des relances proactives ; absente = allowed_hours
    blocked: list[BlockedSlot] = Field(default_factory=list)
    buffer_minutes: int = Field(default=5, ge=0)
    max_block_minutes: int = Field(default=120, gt=0)
    min_block_minutes: int = Field(default=25, gt=0)
    max_focus_hours_per_day: float = Field(default=3, gt=0)
    reminder_lead_minutes: int = Field(default=15, ge=0)
    briefing_time: time = time(7, 45)
    weekly_recap_time: time = time(18, 0)  # dimanche
    max_reschedules: int = Field(default=3, gt=0)
    # jours avant l'échéance où une tâche peut démarrer, selon sa durée estimée ; vide = au plus tôt
    start_lead_days: list[LeadTier] = Field(default_factory=list)

    @field_validator("briefing_time", "weekly_recap_time", mode="before")
    @classmethod
    def _bt(cls, v: object) -> time:
        return _parse_hhmm(v)

    def reminder_window(self, day: date) -> HoursRange:
        """Plage où le bot peut relancer de lui-même (hors messages liés à un bloc ou un rendez-vous)."""
        hours = self.reminder_hours or self.allowed_hours
        return hours.weekend if day.weekday() >= 5 else hours.weekday

    def lead_days(self, minutes: int) -> int | None:
        """Combien de jours avant l'échéance une tâche de cette durée peut démarrer (palier le plus court qui la
        contient) ; None si aucune règle ne s'applique (0 = le jour même de l'échéance)."""
        for tier in sorted(self.start_lead_days, key=lambda t: t.up_to_minutes or float("inf")):
            if tier.up_to_minutes is None or minutes <= tier.up_to_minutes:
                return tier.days
        return None

    @property
    def max_focus_minutes_per_day(self) -> int:
        return int(self.max_focus_hours_per_day * 60)


def load_rules(path: str | Path | None = None) -> Rules:
    p = Path(path) if path else DEFAULT_RULES_PATH
    with p.open(encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return Rules.model_validate(data)
