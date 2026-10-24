import json
from datetime import timedelta

from bot.fakes import FakeCoach, FakeMemory, FakeRouter
from bot.handlers import BotService
from coach import store
from coach.calendar_client import FakeCalendarClient
from coach.db import Database
from coach.extract import ExtractCache, extract_items
from coach.rules import load_rules
from coach.scheduler import CoachScheduler
from tests.coach.conftest import FROZEN, dt

NOW = dt(2026, 10, 15, 9, 0)
EV = {"title": "Rdv médecin", "start": "2026-10-16T15:00:00+02:00", "end": None,
      "location": "Cabinet Dr Martin", "confidence": 0.9}


class LLM:
    def __init__(self, payload): self.payload = payload
    def __call__(self, prompt, *, sensitivity, system=None, json_mode=False): return json.dumps(self.payload)


def test_event_extracted_not_a_task_default_one_hour():
    res = extract_items("rdv médecin vendredi 15h", "note:1", "private",
                        llm=LLM({"tasks": [], "events": [EV]}), cache=ExtractCache(), now=NOW)
    assert res.tasks == []
    (e,) = res.events
    assert e.end - e.start == timedelta(hours=1) and e.location == "Cabinet Dr Martin"


def test_low_confidence_event_dropped_and_cache_keeps_events():
    cache = ExtractCache()
    llm = LLM({"tasks": [], "events": [EV, {**EV, "title": "Peut-être", "confidence": 0.3}]})
    assert len(extract_items("x", "s", "private", llm=llm, cache=cache, now=NOW).events) == 1
    llm.payload = {"tasks": [], "events": []}  # le cache doit répondre, pas le LLM
    assert len(extract_items("x", "s", "private", llm=llm, cache=cache, now=NOW).events) == 1


def make_sched():
    db, sent = Database(":memory:"), []
    cal = FakeCalendarClient()
    s = CoachScheduler(db, cal, load_rules(FROZEN), lambda t, b: sent.append(t), now_fn=lambda: NOW)
    store.configure(db, s)
    return db, cal, sent, s


def test_add_event_exact_time_and_reminder():
    db, cal, sent, s = make_sched()
    e = extract_items("x", "s", "private", llm=LLM({"events": [EV]}), cache=ExtractCache(), now=NOW).events[0]
    assert db.add_event_if_new(e) and not db.add_event_if_new(e)  # pas de doublon
    msg = store.add_event(e.id)
    (fake,) = cal.events.values()
    assert fake.start == e.start and fake.end == e.end and "Cabinet" in msg
    s.tick(e.start - timedelta(minutes=10))
    assert any("Rdv médecin" in m for m in sent)
    n = len(sent)
    s.tick(e.start - timedelta(minutes=9))
    assert len(sent) == n  # rappel envoyé une seule fois


def test_bot_adds_user_events_directly():
    class C(FakeCoach):
        def __init__(self):
            super().__init__()
            self.events = [type("E", (), {"id": "ev1"})()]
    c = C()
    replies = BotService(c, FakeMemory(), FakeRouter(), 1).on_text("rdv médecin vendredi 15h")
    assert ("add_event", "ev1") in c.calls and any("Ajouté" in r.text for r in replies)
