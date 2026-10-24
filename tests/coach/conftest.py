from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from coach.rules import Rules, load_rules

PARIS = ZoneInfo("Europe/Paris")
# copie figée des règles : les tests du planificateur ne dépendent pas de ta configuration (coach/rules.yaml)
FROZEN = Path(__file__).with_name("rules_frozen.yaml")


def dt(y: int, mo: int, d: int, h: int = 0, mi: int = 0) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=PARIS)


@pytest.fixture
def rules() -> Rules:
    return load_rules(FROZEN)
