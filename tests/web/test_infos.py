"""Infos : « séance annulée » n'est ni une tâche ni un rendez-vous, mais une info (message + onglet Infos)."""
import json

import pytest

import ingest_gmail
import memory
from coach import extract, store
from coach.calendar_client import FakeCalendarClient
from coach.db import Database
from coach.rules import load_rules
from coach.scheduler import CoachScheduler
from tests.coach.conftest import FROZEN, dt
from tests.web.test_pages import make
from tests.web.test_server import login, run
from web import data

NOW = dt(2026, 11, 12, 9, 0)
ANSWER = {"tasks": [], "events": [],
          "infos": [{"title": "TD de POO annulé mardi", "detail": "Rattrapage annoncé plus tard.", "day": "2026-11-17",
                     "category": "cours", "importance": 3, "confidence": 0.9}]}


@pytest.fixture
def env(monkeypatch, tmp_path):
    db = Database(":memory:")
    store.configure(db, CoachScheduler(db, FakeCalendarClient(), load_rules(FROZEN), lambda t, b: None, now_fn=lambda: NOW))
    monkeypatch.setattr(extract, "_resolve_llm", lambda: lambda prompt, **k: json.dumps(ANSWER))
    monkeypatch.setattr(memory, "DB_PATH", tmp_path / "m.sqlite")
    monkeypatch.setattr(memory, "_conn", None)
    return db


def test_cancelled_session_becomes_an_info_and_a_plain_message(env):
    sent = []
    ingest_gmail.process_mail("De : Prof <p@insa-lyon.fr>\nObjet : TD annulé\n\nPas de TD mardi.", "TD annulé",
                              "insa:mid:42", lambda text, buttons: sent.append((text, buttons)))
    assert env.list_tasks() == [] and env.all_events() == []          # ni tâche, ni rendez-vous
    (info,) = env.list_infos()
    assert info.title == "TD de POO annulé mardi" and info.day.isoformat() == "2026-11-17" and not info.seen
    assert info.category == "cours" and info.importance == 3 and info.sender == "p@insa-lyon.fr"
    assert sent == [("ℹ️ <b>📚 Cours & examens</b> · mar. 17 nov. : TD de POO annulé mardi\nRattrapage annoncé plus tard.",
                     [[("👎 Pas utile", f"inon:{info.id}")]])]
    store.extract_items("le même mail relu, test infos 42", "insa:mid:42", "private")
    assert len(env.list_infos()) == 1                                   # pas de doublon


def test_infos_tab_lists_and_marks_seen(env):
    store.extract_items("mail unique de test infos onglet 7", "insa:mid:7", "private")
    v = data.Views(env, store._need()[1], data.AgendaCache(lambda s, e: []), clock=lambda: NOW)
    assert v.badges()["infos"] == 1
    view = v.infos()
    first = view["items"]
    assert first[0]["title"] == "TD de POO annulé mardi" and first[0]["new"] and not first[0]["past"]
    assert [(c["key"], c["new"]) for c in view["cases"]] == [("cours", 1)]
    assert v.badges()["infos"] == 0 and not v.infos()["items"][0]["new"]   # vue une fois : plus « Nouveau »


def test_infos_tab_loads_twenty_at_a_time(env):
    from coach.models import Info
    for k in range(45):
        env.add_info(Info(id=f"p{k}", title=f"Info numéro {k}", category="cours"))
    v = data.Views(env, store._need()[1], data.AgendaCache(lambda s, e: []), clock=lambda: NOW)
    page = v.infos()
    assert len(page["items"]) == 20 and page["more"] and page["next"] == 40
    assert env.unseen_infos() == 25                             # seules les infos affichées sont vues
    assert len(v.infos("40")["items"]) == 40 and not v.infos("9999")["more"]
    assert data.info_page_size("abc") == 20 and data.info_page_size("3") == 20


def test_infos_page_renders_with_nav_entry():
    app, db = make()

    async def scenario(c):
        await login(c)
        page = await (await c.get("/infos")).text()
        assert 'href="/infos"' in page and "Aucune info pour l'instant" in page
        frag = await (await c.get("/fragment/infos")).text()
        assert "<nav" not in frag and "Aucune info" in frag
    run(app, scenario)
