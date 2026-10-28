from __future__ import annotations

import logging

import health
from bot.fakes import FakeCoach, FakeMemory, FakeRouter
from bot.handlers import BotService
from coach.calendar_client import FakeCalendarClient
from coach.db import Database
from coach.rules import load_rules
from coach.scheduler import CoachScheduler
from tests.coach.conftest import FROZEN, dt
from web import data, system

import run

NOW = dt(2026, 10, 27, 10, 27)


def test_web_deps_builds_every_section(tmp_path):
    db = Database(":memory:")
    sched = CoachScheduler(db, FakeCalendarClient(), load_rules(FROZEN), lambda t, b=None: None, lambda: NOW)
    svc = BotService(FakeCoach([]), FakeMemory(), FakeRouter("x"), 42)
    hl = health.Health(lambda t, b=None: None, path=tmp_path / "h.json")
    deps = run.web_deps(svc, db, sched, hl, system.ErrorLog())
    assert deps.svc is svc
    assert deps.home().counters.open_tasks == 0
    assert deps.machine().disk_total_gb > 0
    assert any(c.component == "gmail" for c in deps.components())
    assert isinstance(deps.quota(), str)
    assert deps.views.focus() == [] and deps.views.projects() == []
    assert deps.views.agenda(None, "jour")["day"] == NOW.date()
    assert isinstance(deps.views.notes_provider, data.MemoryNotes)        # Notes : la vraie mémoire, en lecture seule


def test_start_web_never_raises(monkeypatch, caplog):
    def boom(*a, **k):
        raise RuntimeError("port déjà pris")
    monkeypatch.setattr(run.web_server, "start", boom)
    with caplog.at_level(logging.ERROR):
        assert run.start_web(None) is None
    assert "page web" in caplog.text
