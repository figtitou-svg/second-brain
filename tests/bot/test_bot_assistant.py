from __future__ import annotations

import health
from bot.fakes import FakeCoach, FakeMemory, FakeRouter
from bot.handlers import BotService
from bot.ports import CoachAdapter
from coach import store
from coach.calendar_client import FakeCalendarClient
from coach.models import Suggestion
from coach.rules import load_rules
from coach.scheduler import CoachScheduler
from tests.coach.assistant_helpers import NOW, make_project
from tests.coach.conftest import FROZEN


def make():
    coach = FakeCoach()
    return BotService(coach, FakeMemory(), FakeRouter(), 42), coach


def test_ps_and_pa_callbacks_are_routed_to_the_assistant():
    svc, coach = make()
    r = svc.on_callback("ps:add:abcd1234")
    assert coach.calls[-1] == ("assistant_action", "ps", "add:abcd1234") and r.text == "assistant ps"
    assert r.buttons == [[("ok", "pjv:x")]]
    svc.on_callback("ps:all:lot00001")
    assert coach.calls[-1] == ("assistant_action", "ps", "all:lot00001")
    svc.on_callback("pa:rng:abcd1234")
    assert coach.calls[-1] == ("assistant_action", "pa", "rng:abcd1234")
    svc.on_callback("pa:ok:" + "t" * 36)
    assert coach.calls[-1] == ("assistant_action", "pa", "ok:" + "t" * 36)


def test_other_callbacks_are_unchanged():
    svc, coach = make()
    svc.on_callback("pjv:abcd1234")
    assert coach.calls[-1] == ("project_action", "pjv", "abcd1234")
    svc.on_callback("pjok:t1")
    assert coach.calls[-1] == ("project_pick", "pjok", "t1", None)


def test_health_label_for_the_scheduled_job():
    assert health.LABELS["projects"] == "Assistant de projet"


def test_unknown_or_dismissed_suggestion_through_the_real_bot_path_never_raises():
    db, p, m1, m2, m3 = make_project()
    sched = CoachScheduler(db, FakeCalendarClient(), load_rules(FROZEN), lambda t, b=None: None, lambda: NOW)
    store.configure(db, sched)
    s = Suggestion(batch="lot00001", project_id=p.id, milestone_id=m2.id, title="Suggestion distincte", created_at=NOW)
    db.save_suggestions([s])
    svc = BotService(CoachAdapter(), FakeMemory(), FakeRouter(), 42)

    unknown = svc.on_callback("ps:add:inconnu1")
    assert "introuvable" in unknown.text and unknown.buttons == []
    assert "introuvable" in svc.on_callback("ps:all:lotinconnu").text
    assert "inconnue" in svc.on_callback("pa:zzz:x").text            # sous-action inconnue : message propre

    svc.on_callback(f"ps:no:{s.id}")                                  # on écarte la suggestion...
    again = svc.on_callback(f"ps:add:{s.id}")                         # ...puis on clique quand même « Ajouter »
    assert "Déjà traité" in again.text
    assert db.get_suggestion(s.id).state == "dismissed"
