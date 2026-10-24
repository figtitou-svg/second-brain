from __future__ import annotations

from datetime import timedelta

from coach import project_assistant, store
from coach.calendar_client import FakeCalendarClient
from coach.db import Database
from coach.models import Project, Suggestion, Task
from coach.rules import load_rules
from coach.scheduler import CoachScheduler
from tests.coach.assistant_helpers import NOW, FakeLLM, make_project, tasks_json
from tests.coach.conftest import FROZEN, dt

GOOD = tasks_json({"title": "Alpha pipeline", "estimated_minutes": 60}, {"title": "Bravo tests"},
                  {"title": "Charlie deploy"})


def env(llm):
    db, p, m1, m2, m3 = make_project()
    sched = CoachScheduler(db, FakeCalendarClient(), load_rules(FROZEN), lambda t, b=None: None, lambda: NOW)
    store.configure(db, sched)
    store.configure_projects(llm, "private", {})
    return db, p, m1, m2, m3


def buttons_of(buttons):
    return [cb for row in buttons for _, cb in row]


# --- étape franchie ------------------------------------------------------------------------

def test_crossing_a_step_adds_suggestions_for_the_next_one():
    llm = FakeLLM(GOOD)
    db, p, m1, m2, m3 = env(llm)
    text, buttons = store.project_action("pm", f"ok:{m2.id}")
    assert "franchie" in text and "💡 Pour la suite de <b>Projet IA</b> (étape <i>Rapport</i>)" in text
    cbs = buttons_of(buttons)
    assert any(c.startswith("ps:add:") for c in cbs) and cbs[-1] == f"pjv:{p.id}"
    assert "Étape visée : Rapport" in llm.calls[0]["prompt"]
    assert db.get_milestone(m2.id).status == "done"


def test_no_suggestions_when_the_llm_is_down_or_nothing_to_say():
    db, p, m1, m2, m3 = env(FakeLLM(RuntimeError("quota")))
    text, buttons = store.project_action("pm", f"ok:{m2.id}")
    assert "franchie" in text and "💡" not in text and buttons_of(buttons) == [f"pjv:{p.id}"]


def test_no_suggestions_after_the_last_step_for_paused_projects_or_pending_batches():
    llm = FakeLLM(GOOD)
    db, p, m1, m2, m3 = env(llm)
    db.save_milestone(m3.model_copy(update={"status": "done"}))            # m2 devient la dernière étape ouverte
    text, _ = store.project_action("pm", f"ok:{m2.id}")
    assert "la dernière" in text and "💡" not in text and llm.calls == []

    llm2 = FakeLLM(GOOD)
    db2, p2, a1, a2, a3 = env(llm2)
    db2.save_project(p2.model_copy(update={"status": "paused"}))
    assert "💡" not in store.project_action("pm", f"ok:{a2.id}")[0] and llm2.calls == []

    llm3 = FakeLLM(GOOD)
    db3, p3, b1, b2, b3 = env(llm3)
    db3.save_suggestions([Suggestion(batch="old", project_id=p3.id, title="En attente", created_at=NOW)])
    assert "💡" not in store.project_action("pm", f"ok:{b2.id}")[0] and llm3.calls == []


def test_double_click_on_a_crossed_step_does_not_ask_again():
    llm = FakeLLM(GOOD)
    db, p, m1, m2, m3 = env(llm)
    store.project_action("pm", f"ok:{m2.id}")
    text, _ = store.project_action("pm", f"ok:{m2.id}")
    assert "déjà franchie" in text and "💡" not in text and len(llm.calls) == 1


# --- projets inactifs ----------------------------------------------------------------------

def idle_env(llm_answers=(GOOD,), **changes):
    db, p, m1, m2, m3 = make_project()
    db.save_project(p.model_copy(update={"last_activity": NOW - timedelta(hours=2), **changes}))
    db.upsert_task(Task(title="[IA] Choisir le modèle", project_id=p.id, milestone_id=m2.id, status="done"))
    llm = FakeLLM(*llm_answers)
    sent = []
    return db, p, m1, m2, m3, llm, sent, lambda text, buttons=None: sent.append((text, buttons))


def run(db, llm, notify, now=NOW):
    return project_assistant.idle_check(db, llm, "private", notify, now)


def test_eligible_project_gets_one_proposal_per_day():
    db, p, m1, m2, m3, llm, sent, notify = idle_env()
    assert run(db, llm, notify) == 1
    text, buttons = sent[0]
    assert "💡 Pour la suite de <b>Projet IA</b> (étape <i>Modèle</i>)" in text
    assert any(c.startswith("ps:add:") for c in buttons_of(buttons))
    assert db.has_notification(f"psuggest:{p.id}:2026-10-21")
    assert run(db, llm, notify) == 0 and len(sent) == 1 and len(llm.calls) == 1


def test_each_condition_blocks_the_proposal():
    for label, kwargs in (("paused", {"status": "paused"}), ("recent", {"last_activity": NOW - timedelta(minutes=30)})):
        db, p, m1, m2, m3, llm, sent, notify = idle_env(**kwargs)
        assert run(db, llm, notify) == 0 and llm.calls == [], label
    db, p, m1, m2, m3, llm, sent, notify = idle_env()
    db.upsert_task(Task(title="[IA] Encore à faire", project_id=p.id, status="pending"))
    assert run(db, llm, notify) == 0 and llm.calls == []                       # une tâche ouverte
    db, p, m1, m2, m3, llm, sent, notify = idle_env()
    for m in (m2, m3):
        db.save_milestone(m.model_copy(update={"status": "done"}))
    assert run(db, llm, notify) == 0 and llm.calls == []                       # toutes les étapes sont franchies
    db, p, m1, m2, m3, llm, sent, notify = idle_env()
    db.save_suggestions([Suggestion(batch="b1", project_id=p.id, title="En attente", created_at=NOW)])
    assert run(db, llm, notify) == 0 and llm.calls == []                       # un lot attend déjà


def test_project_without_steps_is_eligible():
    db = Database(":memory:")
    p = Project(name="Perso", profile={"goal": "Apprendre la guitare"}, last_activity=NOW - timedelta(hours=3))
    db.save_project(p)
    llm, sent = FakeLLM(tasks_json({"title": "Apprendre trois accords"})), []
    assert run(db, llm, lambda t, b=None: sent.append(t)) == 1 and "Étape visée : aucune" in llm.calls[0]["prompt"]


def test_failures_do_not_claim_the_day_and_are_limited_to_three_attempts():
    db, p, m1, m2, m3, llm, sent, notify = idle_env((RuntimeError("quota"), "pas du json", "encore", GOOD))
    assert run(db, llm, notify) == 0 and not db.has_notification(f"psuggest:{p.id}:2026-10-21")
    db2, p2, a1, a2, a3, llm2, sent2, notify2 = idle_env((RuntimeError("1"), RuntimeError("2"), RuntimeError("3"), GOOD))
    for _ in range(3):
        run(db2, llm2, notify2)
    assert len(llm2.calls) == 3 and run(db2, llm2, notify2) == 0 and len(llm2.calls) == 3   # plus d'essai aujourd'hui
    db3, p3, b1, b2, b3, llm3, sent3, notify3 = idle_env((RuntimeError("quota"), GOOD))
    assert run(db3, llm3, notify3) == 0 and run(db3, llm3, notify3) == 1      # le réessai réussit


def test_empty_answer_claims_the_day_to_avoid_asking_the_llm_every_15_minutes():
    db, p, m1, m2, m3, llm, sent, notify = idle_env(({"tasks": []},))
    assert run(db, llm, notify) == 0 and db.has_notification(f"psuggest:{p.id}:2026-10-21")
    assert run(db, llm, notify) == 0 and len(llm.calls) == 1


def test_stale_pending_batch_is_dismissed_then_a_new_proposal_can_be_made():
    db, p, m1, m2, m3, llm, sent, notify = idle_env()
    db.save_suggestions([Suggestion(batch="old", project_id=p.id, title="Vieille idée",
                                    created_at=NOW - timedelta(days=4))])
    assert run(db, llm, notify) == 1
    assert db.get_suggestion(db.list_batch("old")[0].id).state == "dismissed"


def test_idle_check_never_raises_and_a_failing_project_does_not_stop_the_others():
    db = Database(":memory:")
    p1 = Project(name="Premier", last_activity=NOW - timedelta(hours=3))
    p2 = Project(name="Second", last_activity=NOW - timedelta(hours=3))
    db.save_project(p1)
    db.save_project(p2)
    llm, sent, calls = FakeLLM(tasks_json({"title": "Une idée"}), tasks_json({"title": "Une autre idée"})), [], []

    def notify(text, buttons=None):
        calls.append(text)
        if len(calls) == 1:
            raise RuntimeError("telegram hors service")
        sent.append(text)

    assert run(db, llm, notify) == 1 and len(sent) == 1 and len(calls) == 2
    assert not db.has_notification(f"psuggest:{p1.id}:2026-10-21")     # message non parti : jour non consommé
    assert db.pending_batch(p1.id) is None                              # et le lot ne bloque pas le prochain essai


def test_failed_delivery_hides_nothing_and_does_not_burn_the_day():
    # le vrai notify ne lève jamais : il renvoie False quand Telegram refuse le message
    db, p, m1, m2, m3, llm, sent, notify = idle_env((GOOD, tasks_json({"title": "Delta revue"})))
    assert run(db, llm, lambda text, buttons=None: False) == 0
    assert not db.has_notification(f"psuggest:{p.id}:2026-10-21")
    assert db.pending_batch(p.id) is None                  # le lot jamais vu est oublié, il ne bloque plus rien
    assert run(db, llm, notify) == 1 and len(sent) == 1 and len(llm.calls) == 2   # le passage suivant repropose
    assert "Delta revue" in sent[0][0] and db.has_notification(f"psuggest:{p.id}:2026-10-21")


def test_delivery_that_raises_is_handled_like_a_failed_delivery():
    db, p, m1, m2, m3, llm, sent, notify = idle_env((GOOD, tasks_json({"title": "Delta revue"})))

    def boom(text, buttons=None):
        raise RuntimeError("telegram hors service")

    assert run(db, llm, boom) == 0
    assert not db.has_notification(f"psuggest:{p.id}:2026-10-21") and db.pending_batch(p.id) is None
    assert run(db, llm, notify) == 1 and len(llm.calls) == 2


def test_failed_deliveries_are_capped_at_three_llm_calls_a_day():
    db, p, m1, m2, m3, llm, sent, notify = idle_env([tasks_json({"title": f"Idée {i}"}) for i in range(5)])
    for _ in range(5):
        assert run(db, llm, lambda text, buttons=None: False) == 0
    assert len(llm.calls) == 3 and sent == [] and db.pending_batch(p.id) is None
    assert run(db, llm, notify) == 0 and len(llm.calls) == 3          # plus d'essai aujourd'hui


def test_a_notify_that_returns_nothing_counts_as_delivered():
    db, p, m1, m2, m3, llm, sent, notify = idle_env()
    assert run(db, llm, lambda text, buttons=None: None) == 1
    assert db.has_notification(f"psuggest:{p.id}:2026-10-21") and db.pending_batch(p.id) is not None


def test_a_render_failure_forgets_the_batch_too(monkeypatch):
    db, p, m1, m2, m3, llm, sent, notify = idle_env((GOOD, GOOD))
    monkeypatch.setattr(project_assistant, "render_batch", lambda *a, **k: (_ for _ in ()).throw(ValueError("rendu")))
    assert run(db, llm, notify) == 0 and sent == []
    assert not db.has_notification(f"psuggest:{p.id}:2026-10-21") and db.pending_batch(p.id) is None


def test_idle_proposals_wait_for_the_allowed_hours():
    llm = FakeLLM(GOOD)
    db, p, m1, m2, m3 = make_project()
    db.save_project(p.model_copy(update={"last_activity": NOW - timedelta(hours=40)}))
    db.upsert_task(Task(title="[IA] Fait", project_id=p.id, status="done"))
    late, sent = [dt(2026, 10, 21, 23, 30)], []
    sched = CoachScheduler(db, FakeCalendarClient(), load_rules(FROZEN), lambda t, b=None: None, lambda: late[0])
    store.configure(db, sched)
    store.configure_projects(llm, "private", {})
    notify = lambda t, b=None: sent.append(t)  # noqa: E731
    assert store.projects_idle_check(notify) == 0 and llm.calls == [] and sent == []        # la nuit : rien
    late[0] = dt(2026, 10, 22, 7, 59)
    assert store.projects_idle_check(notify) == 0 and llm.calls == []                       # avant 8 h : rien
    late[0] = dt(2026, 10, 22, 8, 0)
    assert store.projects_idle_check(notify) == 1 and len(llm.calls) == 1 and "💡" in sent[0]  # dans les heures
    late[0] = dt(2026, 10, 24, 9, 30)                                                       # samedi : fenêtre 10 h-20 h
    db.set_suggestion_state(db.list_batch(db.pending_batch(p.id))[0].id, "dismissed")
    assert store.projects_idle_check(notify) == 0 and len(llm.calls) == 1


def test_a_failing_stale_cleanup_does_not_stop_the_proposals(monkeypatch):
    db, p, m1, m2, m3, llm, sent, notify = idle_env()

    def boom(cutoff):
        raise RuntimeError("ligne illisible")

    monkeypatch.setattr(db, "dismiss_stale_suggestions", boom)
    assert run(db, llm, notify) == 1 and len(sent) == 1


def test_naive_last_activity_does_not_break_the_check():
    db = Database(":memory:")
    db.save_project(Project(name="Naïf", last_activity=(NOW - timedelta(hours=3)).replace(tzinfo=None)))
    sent = []
    assert run(db, FakeLLM(tasks_json({"title": "Une idée"})), lambda t, b=None: sent.append(t)) == 1


def test_store_idle_check_uses_the_configured_llm():
    llm = FakeLLM(GOOD)
    db, p, m1, m2, m3 = env(llm)
    db.save_project(p.model_copy(update={"last_activity": NOW - timedelta(hours=2)}))
    db.upsert_task(Task(title="[IA] Fait", project_id=p.id, status="done"))
    sent = []
    assert store.projects_idle_check(lambda t, b=None: sent.append(t)) == 1 and "💡" in sent[0]
