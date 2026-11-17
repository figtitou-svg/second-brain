"""Catégories par surnom (« chaps » -> [Cha]) et commande /task."""
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from bot.fakes import FakeMemory, FakeRouter
from bot.handlers import BotService
from bot.ports import CoachAdapter
from coach import categories, extract, store
from coach.calendar_client import FakeCalendarClient
from coach.db import Database
from coach.rules import load_rules
from coach.scheduler import CoachScheduler
from tests.coach.conftest import FROZEN

TZ = ZoneInfo("Europe/Paris")
NOW = datetime(2026, 11, 17, 9, 0, tzinfo=TZ)


def test_guess_by_alias_not_by_bare_code():
    cats = categories.known()
    assert categories.guess("rajoute dans chaps le fait de recontacter PE", cats) == "Cha"
    assert categories.guess("la semaine pro je dois appeler", cats) is None      # « pro » n'est pas [Pro]
    assert categories.guess("[Pro] finir le TP", cats) == "Pro"
    assert categories.guess("chaps et franck", cats) is None                     # deux catégories : on ne devine pas
    assert categories.prefixed("[Esp] Fiche", "Cha") == "[Esp] Fiche" and categories.prefixed("Fiche", "Cha") == "[Cha] Fiche"


@pytest.fixture
def env(monkeypatch):
    db = Database(":memory:")
    store.configure(db, CoachScheduler(db, FakeCalendarClient(), load_rules(FROZEN), lambda t, b=None: None,
                                       now_fn=lambda: NOW))
    seen = []

    def llm(prompt, **k):
        seen.append((prompt, k.get("system", "")))
        if "PE" in prompt:
            return json.dumps({"tasks": [{"title": "Recontacter PE au sujet du partenariat", "category": "cha",
                                          "estimated_minutes": 15}], "events": [], "infos": []})
        return json.dumps({"tasks": [], "events": [], "infos": []})
    monkeypatch.setattr(extract, "_resolve_llm", lambda: llm)
    return db, seen


def test_free_text_lands_in_its_category(env):
    db, seen = env
    tasks, _ = store.extract_items("rajoute dans chaps le fait que je doive recontacter PE au sujet du partenariat",
                                   "note:1", "private")
    assert [t.title for t in tasks] == ["[Cha] Recontacter PE au sujet du partenariat"]
    assert "Cha : CHAPS" in seen[0][0] and "category" in seen[0][1]


def test_task_command_creates_and_places(env):
    db, _ = env
    svc = BotService(CoachAdapter(), FakeMemory(), FakeRouter("x"), 1)
    assert "Écris la tâche après /task" in svc.task("  ")[0].text
    (card,) = svc.task("recontacter PE pour le partenariat chaps")
    assert "Tâche ajoutée" in card.text and "[Cha] Recontacter PE" in card.text and "Planifiée" in card.text
    (card,) = svc.task("acheter des crampons")                              # l'IA ne voit rien : texte tel quel
    assert "Acheter des crampons" in card.text
    assert {t.title for t in db.list_tasks("pending", "scheduled")} >= {"Acheter des crampons"}
