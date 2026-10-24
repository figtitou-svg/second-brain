from __future__ import annotations

from datetime import date, timedelta

from coach import project_watch
from coach.models import Milestone, Project, Task
from tests.coach.conftest import dt

NOW = dt(2026, 10, 21, 10)  # mercredi


def proj(**fields):
    fields.setdefault("last_activity", NOW)
    fields.setdefault("name", "Projet IA")
    return Project(category="IA", **fields)


def step(p, title, position, due=None, status="open"):
    return Milestone(project_id=p.id, title=title, position=position, due=due, status=status)


def work(p, minutes=30, status="pending"):
    return Task(title=f"[IA] tâche de {minutes} min", project_id=p.id, estimated_minutes=minutes, status=status)


def codes(stall):
    return [s.code for s in stall.signals]


def test_a_fresh_project_is_not_flagged():
    p = proj(deadline=date(2026, 12, 15))
    assert project_watch.assess(p, [step(p, "Modèle", 0, date(2026, 11, 20))], [work(p)], NOW) is None


def test_inactivity_starts_at_seven_days():
    assert project_watch.assess(proj(last_activity=NOW - timedelta(days=6)), [], [], NOW) is None
    stall = project_watch.assess(proj(last_activity=NOW - timedelta(days=7)), [], [], NOW)
    assert codes(stall) == ["inactive"] and stall.signals[0].line == "rien n'a bougé depuis 7 jours"
    assert stall.severity == "inactive" and stall.last_date == date(2026, 10, 14)
    assert stall.situation == "2026-10-14:inactive"


def test_missing_last_activity_falls_back_to_creation_date():
    young = Project(name="Neuf", created_at=NOW - timedelta(days=3))
    assert project_watch.assess(young, [], [], NOW) is None
    old = Project(name="Vieux", created_at=NOW - timedelta(days=8))
    assert project_watch.assess(old, [], [], NOW).signals[0].line == "rien n'a bougé depuis 8 jours"


def test_naive_last_activity_is_read_in_the_planning_timezone():
    p = proj(last_activity=(NOW - timedelta(days=9)).replace(tzinfo=None))
    assert project_watch.assess(p, [], [], NOW).signals[0].line == "rien n'a bougé depuis 9 jours"


def test_deadline_in_the_past_is_overdue_but_today_is_not():
    stall = project_watch.assess(proj(deadline=date(2026, 10, 20)), [], [], NOW)
    assert codes(stall) == ["overdue"] and stall.signals[0].line == "la date de rendu est dépassée"
    assert project_watch.assess(proj(deadline=date(2026, 10, 21)), [], [], NOW) is None


def test_late_step_only_looks_at_the_current_open_step():
    p = proj()
    done_late = step(p, "Données", 0, date(2026, 9, 1), status="done")
    later = step(p, "Rapport", 2, date(2026, 9, 5))
    current = step(p, "Modèle <v2>", 1, date(2026, 10, 20))
    stall = project_watch.assess(p, [later, current, done_late], [], NOW)
    assert codes(stall) == ["late_step"]
    assert stall.signals[0].line == "l'étape <i>Modèle &lt;v2&gt;</i> devait finir le 20/10"
    on_time = step(p, "Modèle", 1, date(2026, 10, 21))
    assert project_watch.assess(p, [done_late, on_time, later], [], NOW) is None   # aujourd'hui : pas en retard
    no_date = step(p, "Modèle", 1)
    assert project_watch.assess(p, [done_late, no_date, later], [], NOW) is None   # l'étape en cours n'a pas de date


def test_deadline_at_risk_when_remaining_work_exceeds_the_daily_capacity():
    p = proj(deadline=date(2026, 10, 31))                 # dans 10 jours : 900 min de capacité
    ten = [work(p, 90) for _ in range(10)]
    assert project_watch.assess(p, [], ten, NOW) is None  # 900 min : pile la capacité
    stall = project_watch.assess(p, [], ten + [work(p, 1)], NOW)
    assert codes(stall) == ["at_risk"]
    assert stall.signals[0].line == "il reste 15 h 01 de travail pour 10 jours"


def heavy(p):
    return [work(p, 480) for _ in range(6)]               # 2880 min


def test_at_risk_only_inside_the_horizon():
    p30 = proj(deadline=date(2026, 11, 20))               # dans 30 jours : 2700 min de capacité
    assert codes(project_watch.assess(p30, [], heavy(p30), NOW)) == ["at_risk"]
    p31 = proj(deadline=date(2026, 11, 21))               # dans 31 jours : hors horizon
    assert project_watch.assess(p31, [], heavy(p31), NOW) is None


def test_imminent_deadline_with_open_work_is_at_risk():
    p = proj(deadline=date(2026, 10, 22))
    stall = project_watch.assess(p, [], [work(p, 30)], NOW)
    assert stall.signals[0].line == "il reste 30 min de travail pour 1 jour"
    today = proj(deadline=date(2026, 10, 21))
    stall = project_watch.assess(today, [], [work(today, 30)], NOW)
    assert stall.signals[0].line == "il reste 30 min de travail pour aujourd'hui"
    assert project_watch.assess(p, [], [], NOW) is None                                   # rien à faire : pas de risque
    assert project_watch.assess(p, [], [work(p, 30, "done"), work(p, 30, "dropped")], NOW) is None


def test_remaining_work_caps_each_task_at_480_minutes():
    p = proj(deadline=date(2026, 10, 31))
    stall = project_watch.assess(p, [], [work(p, 9999), work(p, 9999)], NOW)
    assert stall.signals[0].line == "il reste 16 h de travail pour 10 jours"


def test_signals_are_ordered_by_severity():
    p = proj(deadline=date(2026, 10, 20), last_activity=NOW - timedelta(days=9))
    stall = project_watch.assess(p, [step(p, "Modèle", 0, date(2026, 10, 1))], [work(p)], NOW)
    assert codes(stall) == ["overdue", "late_step", "inactive"] and stall.severity == "overdue"


def test_no_deadline_means_only_step_and_inactivity_signals():
    p = proj(last_activity=NOW - timedelta(days=8))
    stall = project_watch.assess(p, [step(p, "Modèle", 0, date(2026, 10, 1))], [work(p, 4000)], NOW)
    assert codes(stall) == ["late_step", "inactive"]


def test_paused_and_finished_projects_are_never_flagged():
    for status in ("paused", "done"):
        p = proj(status=status, deadline=date(2026, 10, 1), last_activity=NOW - timedelta(days=30))
        assert project_watch.assess(p, [], [], NOW) is None


def test_alert_text_and_buttons():
    p = proj(name="<b>Gros</b> projet", deadline=date(2026, 10, 20), last_activity=NOW - timedelta(days=9))
    text = project_watch.alert_text(project_watch.assess(p, [], [], NOW))
    assert text == ("⚠️ <b>&lt;b&gt;Gros&lt;/b&gt; projet</b> :\n• la date de rendu est dépassée\n"
                    "• rien n'a bougé depuis 9 jours")
    assert project_watch.alert_buttons(p.id) == [[("⏸ Pause", f"pp:pause:{p.id}"), ("💡 Relancer", f"ps:go:{p.id}"),
                                                  ("📂 Voir le projet", f"pjv:{p.id}")]]
    assert all(len(cb.encode()) <= 64 for _, cb in project_watch.alert_buttons("p" * 36)[0])
    long_name = proj(name="N" * 200, last_activity=NOW - timedelta(days=9))
    long_text = project_watch.alert_text(project_watch.assess(long_name, [], [], NOW))
    assert "N" * 80 in long_text and "N" * 81 not in long_text
