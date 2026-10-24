from __future__ import annotations

from datetime import date, timedelta

import health
from coach import project_watch, store
from coach.calendar_client import FakeCalendarClient
from coach.db import Database
from coach.models import Project, Suggestion, Task
from coach.rules import load_rules
from coach.scheduler import CoachScheduler
from tests.coach.conftest import FROZEN, dt

NOW = dt(2026, 10, 21, 10)  # mercredi


def day(n):
    return NOW + timedelta(days=n)


class Outbox:
    """notify de test : garde les messages ; `result` vaut True (envoyé), False (échec) ou une exception à lever."""

    def __init__(self, result=True):
        self.sent, self.result = [], result

    def __call__(self, text, buttons=None):
        if isinstance(self.result, Exception):
            raise self.result
        if self.result is False:
            return False
        self.sent.append((text, buttons))
        return True


def stalled(db, name="Projet IA", days=9, open_task=True, **fields):
    """Projet sans activité depuis `days` jours ; avec une tâche ouverte (sans elle, un projet inactif relève de B1)."""
    p = Project(name=name, last_activity=NOW - timedelta(days=days), **fields)
    db.save_project(p)
    if open_task:
        db.upsert_task(Task(title=f"[IA] Tâche de {name}", project_id=p.id))
    return p


def names(out):
    return [t.split("</b>")[0].split("<b>")[1] for t, _ in out.sent]


def test_a_stalled_project_gets_one_alert_with_its_buttons_and_keys():
    db = Database(":memory:")
    p = stalled(db)
    out = Outbox()
    assert project_watch.watch(db, out, NOW) == 1
    text, buttons = out.sent[0]
    assert text.startswith("⚠️ <b>Projet IA</b> :") and "rien n'a bougé depuis 9 jours" in text
    assert [cb for _, cb in buttons[0]] == [f"pp:pause:{p.id}", f"ps:go:{p.id}", f"pjv:{p.id}"]
    sit = "2026-10-12:inactive"
    assert db.has_notification("pwatch:day:2026-10-21")
    assert db.has_notification(f"pwalert:{p.id}:inactive:2026-10-21")
    assert db.has_notification(f"pwslot:{p.id}:{sit}:1")
    assert project_watch.watch(db, out, NOW) == 0 and len(out.sent) == 1     # un seul message par jour


def test_nothing_to_say_sends_nothing():
    db = Database(":memory:")
    db.save_project(Project(name="Frais", last_activity=NOW))
    out = Outbox()
    assert project_watch.watch(db, out, NOW) == 0 and out.sent == []
    assert not db.has_notification("pwatch:day:2026-10-21")


def test_one_message_per_day_most_serious_first_then_oldest():
    db = Database(":memory:")
    stalled(db, "Alpha", days=9)
    stalled(db, "Bravo", days=1, deadline=date(2026, 10, 20))     # rendu dépassé : le plus grave
    stalled(db, "Charlie", days=12)
    out = Outbox()
    for n in range(3):
        assert project_watch.watch(db, out, day(n)) == 1
    assert names(out) == ["Bravo", "Charlie", "Alpha"]


def test_same_situation_is_repeated_every_four_days_at_most_three_times():
    db = Database(":memory:")
    stalled(db)
    out = Outbox()
    sent_on = [n for n in range(0, 16) if project_watch.watch(db, out, day(n))]
    assert sent_on == [0, 4, 8]


def test_new_activity_or_a_worse_problem_starts_a_new_situation():
    db = Database(":memory:")
    p = stalled(db)
    out = Outbox()
    assert [project_watch.watch(db, out, day(n)) for n in (0, 4, 8)] == [1, 1, 1]
    assert project_watch.watch(db, out, day(12)) == 0                        # trois alertes déjà envoyées
    db.save_project(p.model_copy(update={"last_activity": day(8)}))          # le projet a bougé
    assert project_watch.watch(db, out, day(14)) == 0                        # 6 jours : plus inactif
    assert project_watch.watch(db, out, day(15)) == 1                        # 7 jours : nouvelle situation
    db.save_project(p.model_copy(update={"last_activity": day(8), "deadline": date(2026, 10, 25)}))
    assert project_watch.watch(db, out, day(16)) == 1                        # rendu dépassé : plus grave, sans attendre


def test_new_activity_does_not_restart_the_four_day_spacing():
    db = Database(":memory:")
    p = stalled(db)
    out = Outbox()
    assert project_watch.watch(db, out, day(0)) == 1
    db.save_project(p.model_copy(update={"last_activity": day(-6)}))         # touché, mais de nouveau inactif dès J+1
    assert [project_watch.watch(db, out, day(n)) for n in (1, 2, 3)] == [0, 0, 0]
    assert project_watch.watch(db, out, day(4)) == 1                         # 4 jours après la dernière alerte
    assert db.has_notification(f"pwslot:{p.id}:{day(-6).date().isoformat()}:inactive:1")   # compteur neuf


def test_inactive_project_without_open_task_is_left_to_the_idle_job():
    db = Database(":memory:")
    stalled(db, "Vide", open_task=False)
    out = Outbox()
    assert project_watch.watch(db, out, NOW) == 0 and out.sent == []
    stalled(db, "Occupé")
    assert project_watch.watch(db, out, NOW) == 1 and names(out) == ["Occupé"]


def test_overdue_project_without_open_task_is_still_alerted():
    db = Database(":memory:")
    stalled(db, "Vide", open_task=False, deadline=date(2026, 10, 20))
    out = Outbox()
    assert project_watch.watch(db, out, NOW) == 1 and names(out) == ["Vide"]


def test_pending_suggestions_and_paused_projects_do_not_alert():
    db = Database(":memory:")
    a = stalled(db, "Alpha")
    stalled(db, "Bravo", status="paused")
    db.save_suggestions([Suggestion(batch="b1", project_id=a.id, title="En attente", created_at=NOW)])
    out = Outbox()
    assert project_watch.watch(db, out, NOW) == 0 and out.sent == []


def test_failed_delivery_claims_nothing_and_is_retried():
    db = Database(":memory:")
    p = stalled(db)
    for failing in (Outbox(False), Outbox(RuntimeError("réseau"))):
        assert project_watch.watch(db, failing, NOW) == 0
        assert not db.has_notification("pwatch:day:2026-10-21")
        assert not db.has_notification(f"pwslot:{p.id}:2026-10-12:inactive:1")
        assert not db.has_notification(f"pwalert:{p.id}:inactive:2026-10-21")
    ok = Outbox()
    assert project_watch.watch(db, ok, NOW) == 1 and len(ok.sent) == 1


def test_after_a_failed_delivery_the_next_candidate_is_still_alerted():
    db = Database(":memory:")
    stalled(db, "Bravo", days=1, deadline=date(2026, 10, 20))     # le plus grave, mais son envoi échoue
    stalled(db, "Alpha", days=9)
    sent = []

    def notify(text, buttons=None):
        if "Bravo" in text:
            return False
        sent.append(text)
        return True

    assert project_watch.watch(db, notify, NOW) == 1 and len(sent) == 1 and "Alpha" in sent[0]


def test_a_database_error_while_claiming_keys_does_not_raise(monkeypatch):
    db = Database(":memory:")
    stalled(db)

    def boom(key):
        raise RuntimeError("base verrouillée")

    monkeypatch.setattr(db, "claim_notification", boom)
    out = Outbox()
    assert project_watch.watch(db, out, NOW) == 1 and len(out.sent) == 1      # le message est bien parti


def test_a_failing_project_does_not_stop_the_others(monkeypatch):
    db = Database(":memory:")
    stalled(db, "Boom")
    stalled(db, "Alpha")
    real = project_watch.assess

    def flaky(project, milestones, tasks, now):
        if project.name == "Boom":
            raise RuntimeError("données illisibles")
        return real(project, milestones, tasks, now)

    monkeypatch.setattr(project_watch, "assess", flaky)
    out = Outbox()
    assert project_watch.watch(db, out, NOW) == 1 and names(out) == ["Alpha"]


def test_watch_never_raises(monkeypatch):
    db = Database(":memory:")
    stalled(db)

    def boom(*statuses):
        raise RuntimeError("base verrouillée")

    monkeypatch.setattr(db, "list_projects", boom)
    assert project_watch.watch(db, Outbox(), NOW) == 0


def clock_env(clock):
    db = Database(":memory:")
    stalled(db)
    sched = CoachScheduler(db, FakeCalendarClient(), load_rules(FROZEN), lambda t, b=None: None, lambda: clock[0])
    store.configure(db, sched)
    return db


def test_store_watch_waits_for_the_allowed_hours():
    clock = [dt(2026, 10, 21, 23, 30)]
    clock_env(clock)
    out = Outbox()
    assert store.projects_watch(out) == 0                                    # la nuit : rien
    clock[0] = dt(2026, 10, 22, 7, 59)
    assert store.projects_watch(out) == 0                                    # avant 8 h : rien
    clock[0] = dt(2026, 10, 24, 9, 30)
    assert store.projects_watch(out) == 0                                    # samedi : fenêtre 10 h-20 h
    clock[0] = dt(2026, 10, 24, 10, 0)
    assert store.projects_watch(out) == 1 and names(out) == ["Projet IA"]


def test_store_watch_weekday_window_opens_at_eight():
    clock = [dt(2026, 10, 22, 8, 0)]
    clock_env(clock)
    assert store.projects_watch(Outbox()) == 1


def test_health_label_for_the_scheduled_job():
    assert health.LABELS["pwatch"] == "Surveillance des projets"
