"""Règle absolue : une même tâche / un même rendez-vous n'est jamais créé deux fois."""
import json

import pytest

from coach import dedupe, extract, store
from coach.calendar_client import FakeCalendarClient, stable_id
from coach.db import Database
from coach.models import Block, Event, Task
from coach.rules import load_rules
from coach.scheduler import CoachScheduler
from tests.coach.conftest import FROZEN, dt

NOW = dt(2026, 10, 15, 9, 0)


def task(title, source="gmail:1", **kw):
    return Task(id=extract._task_id(source, title), title=title, source_id=source, due=dt(2026, 10, 20, 12), **kw)


def make_sched(payload):
    db, sent = Database(":memory:"), []
    s = CoachScheduler(db, FakeCalendarClient(), load_rules(FROZEN), lambda t, b: sent.append(t), now_fn=lambda: NOW)
    store.configure(db, s)

    def llm(prompt, *, sensitivity, system=None, json_mode=False):
        return json.dumps(payload)

    store_llm = extract._resolve_llm
    extract._resolve_llm = lambda: llm
    return db, s, lambda: setattr(extract, "_resolve_llm", store_llm)


@pytest.mark.parametrize("a,b", [
    ("[IA] Faire license", "Faire la license"),
    ("Répondre au mail de Franck", "Répondre au mail de Franck !"),
    ("Préparer l'oral d'anglais", "preparer loral d anglais"),
    ("[BDE] Mail bde", "Mail BDE"),
])
def test_same_task_despite_wording(a, b):
    assert dedupe.same_title(a, b)


@pytest.mark.parametrize("a,b", [
    ("Rendre le rapport de physique", "Rendre le rapport de chimie"),
    ("Faire le CV", "Faire les courses"),
])
def test_different_tasks_are_kept(a, b):
    assert not dedupe.same_title(a, b)


@pytest.mark.parametrize("title", [
    "Compléter mes informations",
    "Remplir mes informations personnelles",
    "Répondre à l'invitation du rdv Bard",
    "Décliner l'invitation si vous ne pouvez pas y assister",
])
def test_paperwork_is_not_a_task(title):
    assert dedupe.is_not_a_task(title)


@pytest.mark.parametrize("title", ["Rendre le rapport", "Mettre à jour les profils LinkedIn", "Faire mon CV"])
def test_real_tasks_pass(title):
    assert not dedupe.is_not_a_task(title)


def test_same_task_from_another_mail_or_after_done_is_not_recreated():
    db, s, restore = make_sched({"tasks": [{"title": "Faire la license", "due": None, "estimated_minutes": 60,
                                            "priority": "medium", "confidence": 0.9}], "events": []})
    try:
        db.upsert_task(task("[IA] Faire license", source="notion:abc", status="done"))  # même faite : on ne la refait pas
        tasks, _ = store.extract_items("autre mail", "gmail:999", "private")
        assert tasks == []
        assert len(db.list_tasks()) == 1
    finally:
        restore()


def test_one_task_proposed_once_even_if_text_repeats_it():
    db, s, restore = make_sched({"tasks": [
        {"title": "Envoyer le dossier", "due": None, "estimated_minutes": 30, "priority": "medium", "confidence": 0.9},
        {"title": "Envoyer le dossier !", "due": None, "estimated_minutes": 30, "priority": "medium", "confidence": 0.9},
    ], "events": []})
    try:
        tasks, _ = store.extract_items("mail", "gmail:1", "private")
        assert len(tasks) == 1
        assert store.extract_items("le même mail relu", "gmail:2", "private")[0] == []
    finally:
        restore()


def test_same_event_from_two_sources_is_proposed_once():
    ev = {"title": "Rdv médecin", "start": "2026-10-16T15:00:00+02:00", "end": None, "location": None, "confidence": 0.9}
    db, s, restore = make_sched({"tasks": [], "events": [ev]})
    try:
        assert len(store.extract_items("note", "note:1", "private")[1]) == 1
        assert store.extract_items("mail de confirmation", "gmail:2", "private")[1] == []
        assert len(db.all_events()) == 1
    finally:
        restore()


def test_adding_event_twice_or_already_in_calendar_creates_one_event():
    ev = {"title": "Rdv médecin", "start": "2026-10-16T15:00:00+02:00", "end": None, "location": None, "confidence": 0.9}
    db, s, restore = make_sched({"tasks": [], "events": [ev]})
    try:
        (e,) = store.extract_items("note", "note:1", "private")[1]
        store.add_event(e.id)
        store.add_event(e.id)  # double clic
        assert len(s.calendar.events) == 1
    finally:
        restore()


def test_scheduling_twice_does_not_duplicate_blocks():
    db, s, restore = make_sched({"tasks": [], "events": []})
    try:
        t = task("Rapport", source="note:1")
        db.upsert_task(t)
        store.schedule(t.id)
        n = len(s.calendar.events)
        assert n >= 1
        assert "Déjà planifiée" in store.schedule(t.id)
        assert len(s.calendar.events) == n
    finally:
        restore()


def test_calendar_ids_are_stable_so_recreating_never_duplicates():
    cal = FakeCalendarClient()
    t = task("Rapport", source="notion:1")
    b = Block(task_id=t.id, start=dt(2026, 10, 16, 10), end=dt(2026, 10, 16, 11))
    assert cal.create_deadline_marker(t) == cal.create_deadline_marker(t)
    assert cal.create_event(b, t) == cal.create_event(b, t)
    assert len(cal.markers) == 1 and len(cal.events) == 1
    assert stable_id("marker", t.id) == cal.create_deadline_marker(t)


def test_fixed_event_id_is_stable():
    cal = FakeCalendarClient()
    e = Event(title="Rdv", start=dt(2026, 10, 16, 15), end=dt(2026, 10, 16, 16))
    assert cal.create_fixed_event(e) == cal.create_fixed_event(e)
    assert len(cal.events) == 1


def test_event_already_noted_in_calendar_is_not_proposed_again():
    ev = {"title": "Rendez-vous chez La Péhair", "start": "2026-10-17T14:30:00+02:00", "end": None,
          "location": None, "confidence": 0.9}
    db, s, restore = make_sched({"tasks": [], "events": [ev]})
    try:  # le rendez-vous est déjà dans l'agenda, noté à la main sous un autre titre
        s.calendar.external_busy.append((dt(2026, 10, 17, 14, 30), dt(2026, 10, 17, 15, 30)))
        assert store.extract_items("mail de rappel", "gmail:7", "private")[1] == []
        assert store.extract_items("mail de rappel bis", "gmail:8", "private")[1] == []
        assert s.calendar.events == {}  # et il n'est pas recréé non plus
    finally:
        restore()
