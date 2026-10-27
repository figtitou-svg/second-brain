# tests/web/test_data.py
from __future__ import annotations

from datetime import date, timedelta

from coach.calendar_client import FakeCalendarClient
from coach.db import Database
from coach.models import Block, Event, Milestone, Project, Suggestion, Task
from coach.rules import load_rules
from coach.scheduler import CoachScheduler
from tests.coach.conftest import FROZEN, dt
from web import data

NOW = dt(2026, 10, 27, 10, 27)   # lundi


def env(now=NOW, busy=None):
    db = Database(":memory:")
    cal = FakeCalendarClient(busy or [])
    sched = CoachScheduler(db, cal, load_rules(FROZEN), lambda t, b=None: None, lambda: now)
    return db, sched, data.AgendaCache(cal.agenda)


def test_project_color_is_stable_and_in_palette():
    assert data.project_color("abc123") == data.project_color("abc123")
    assert data.project_color("abc123") in data.PALETTE
    assert len({data.project_color(f"p{i}") for i in range(40)}) > 1


def test_project_colors_distinct_for_eight_and_stable_when_one_is_done():
    db, sched, cache = env()
    ps = [Project(name=f"P{i}", created_at=NOW - timedelta(days=10 - i)) for i in range(8)]
    for p in ps:
        db.save_project(p)
    colors = data.project_colors(db)
    assert len(set(colors.values())) == 8 and set(colors.values()) == set(data.PALETTE)
    ps[2].status = "done"
    db.save_project(ps[2])
    assert data.project_colors(db) == colors
    rings = {r.project_id: r.color for r in data.build_home(db, sched, cache, NOW).rings}
    assert ps[2].id not in rings and all(rings[p.id] == colors[p.id] for p in ps if p is not ps[2])


def test_project_colors_ninth_project_wraps_around():
    db, _, _ = env()
    ps = [Project(name=f"P{i}", created_at=NOW - timedelta(days=10 - i)) for i in range(9)]
    for p in ps:
        db.save_project(p)
    colors = data.project_colors(db)
    assert colors[ps[8].id] == data.PALETTE[0] == colors[ps[0].id]


def test_task_without_project_gets_no_project_color():
    db, sched, cache = env()
    db.save_project(Project(name="Projet IA"))
    t = Task(title="Sans projet", status="scheduled")
    db.upsert_task(t)
    db.add_block(Block(task_id=t.id, start=dt(2026, 10, 27, 9, 30), end=dt(2026, 10, 27, 11)))
    home = data.build_home(db, sched, cache, NOW)
    assert home.now_card.color == data.NO_PROJECT and [s.color for s in home.segments] == [data.NO_PROJECT]


def test_home_empty_db():
    db, sched, cache = env()
    home = data.build_home(db, sched, cache, NOW)
    assert home.now_card is None and home.segments == [] and home.focus == [] and home.rings == []
    assert home.counters == data.Counters(0, 0, 0)
    assert 0 <= home.now_pct <= 100


def test_now_card_current_block_and_next():
    db, sched, cache = env()
    p = Project(name="Projet IA")
    db.save_project(p)
    a = Task(title="[IA] Entraîner le modèle", project_id=p.id, status="scheduled")
    b = Task(title="Relire le rapport", status="scheduled")
    for t in (a, b):
        db.upsert_task(t)
    db.add_block(Block(task_id=a.id, start=dt(2026, 10, 27, 9, 30), end=dt(2026, 10, 27, 11, 19)))
    db.add_block(Block(task_id=b.id, start=dt(2026, 10, 27, 14), end=dt(2026, 10, 27, 14, 40)))
    card = data.build_home(db, sched, cache, NOW).now_card
    assert card.current and card.title == "[IA] Entraîner le modèle" and card.minutes_left == 52
    assert card.color == data.project_colors(db)[p.id]
    assert card.next_title == "Relire le rapport" and card.next_start == dt(2026, 10, 27, 14)


def test_now_card_falls_back_to_next_block():
    db, sched, cache = env()
    t = Task(title="Relire le rapport", status="scheduled")
    db.upsert_task(t)
    db.add_block(Block(task_id=t.id, start=dt(2026, 10, 27, 14), end=dt(2026, 10, 27, 14, 40)))
    card = data.build_home(db, sched, cache, NOW).now_card
    assert not card.current and card.title == "Relire le rapport" and card.start == dt(2026, 10, 27, 14)
    assert card.next_title is None


def test_ribbon_segments_kinds_and_colors():
    db, sched, cache = env(busy=[(dt(2026, 10, 27, 8), dt(2026, 10, 27, 10))])
    t = Task(title="Sans projet", status="scheduled")
    db.upsert_task(t)
    db.add_block(Block(task_id=t.id, start=dt(2026, 10, 27, 10), end=dt(2026, 10, 27, 11)))
    db.add_event_if_new(Event(title="Dentiste", start=dt(2026, 10, 27, 17), end=dt(2026, 10, 27, 18)))
    db.set_event_status(db.all_events()[0].id, "added")
    home = data.build_home(db, sched, cache, NOW)
    kinds = sorted((s.kind, s.color) for s in home.segments)
    assert kinds == [("block", data.NO_PROJECT), ("course", data.COURSE), ("event", data.COURSE)]
    assert all(0 <= s.left <= 100 and 0 < s.width <= 100 and s.left + s.width <= 100.001 for s in home.segments)


def test_ribbon_clips_to_window():
    db, sched, cache = env()
    t = Task(title="Tôt", status="scheduled")
    db.upsert_task(t)
    db.add_block(Block(task_id=t.id, start=dt(2026, 10, 27, 7, 30), end=dt(2026, 10, 27, 8, 30)))
    db.add_block(Block(task_id=t.id, start=dt(2026, 10, 27, 23), end=dt(2026, 10, 27, 23, 30)))   # hors plage
    home = data.build_home(db, sched, cache, NOW)
    (seg,) = home.segments
    assert seg.left == 0 and 0 < seg.width < 10
    late = data.build_home(db, sched, cache, dt(2026, 10, 27, 23, 50))
    assert late.now_pct == 100


def test_rings_progress_alert_and_counters():
    db, sched, cache = env()
    p = Project(name="Projet IA", deadline=date(2026, 12, 15), last_activity=NOW)
    q = Project(name="Recherche de job", last_activity=NOW - timedelta(days=10))
    db.save_project(p)
    db.save_project(q)
    for i, st in enumerate(("done", "open", "open")):
        db.save_milestone(Milestone(project_id=p.id, title=f"É{i}", position=i, status=st))
    db.upsert_task(Task(title="[IA] fait", project_id=q.id, status="done"))
    db.upsert_task(Task(title="ouverte", project_id=q.id))
    db.upsert_task(Task(title="en retard", due=NOW - timedelta(days=1)))
    db.save_suggestions([Suggestion(batch="b1", project_id=p.id, title="Idée", created_at=NOW)])
    home = data.build_home(db, sched, cache, NOW)
    rings = {r.name: r for r in home.rings}
    assert rings["Projet IA"].percent == 33 and rings["Projet IA"].days_left == 49
    assert rings["Recherche de job"].percent == 50
    assert rings["Recherche de job"].alert and not rings["Projet IA"].alert
    assert home.counters == data.Counters(open_tasks=2, overdue=1, suggestions=1)


def test_focus_top_three_with_colors():
    db, sched, cache = env()
    p = Project(name="Projet IA")
    db.save_project(p)
    for i in range(5):
        db.upsert_task(Task(title=f"T{i}", project_id=p.id if i == 0 else None, due=NOW + timedelta(days=i + 1)))
    focus = data.build_home(db, sched, cache, NOW).focus
    assert [f.rank for f in focus] == [1, 2, 3]
    assert {f.color for f in focus} == {data.project_colors(db)[p.id], data.NO_PROJECT}


def test_agenda_cache_calls_once_per_ttl_and_survives_errors():
    calls = []

    def agenda(start, end):
        calls.append(start)
        if len(calls) == 2:
            raise RuntimeError("Google indisponible")
        return [(start, end, "Cours")]

    t = [0.0]
    cache = data.AgendaCache(agenda, ttl=300, clock=lambda: t[0])
    s, e = dt(2026, 10, 27, 8), dt(2026, 10, 27, 22)
    assert cache.get(s, e)[0][2] == "Cours"
    assert cache.get(s, e)[0][2] == "Cours" and len(calls) == 1       # en cache
    t[0] = 301
    assert cache.get(s, e)[0][2] == "Cours" and len(calls) == 2       # erreur : dernière valeur connue


def test_agenda_cache_error_without_previous_value_gives_empty():
    cache = data.AgendaCache(lambda s, e: (_ for _ in ()).throw(RuntimeError("panne")))
    assert cache.get(dt(2026, 10, 27, 8), dt(2026, 10, 27, 22)) == []


def _flaky_cache():
    calls = []

    def agenda(start, end):
        calls.append(start)
        if len(calls) >= 2:
            raise RuntimeError("Google indisponible")
        return [(start, end, "Cours")]

    t = [0.0]
    return data.AgendaCache(agenda, ttl=300, retry=60, clock=lambda: t[0]), calls, t


def test_agenda_cache_failure_is_held_for_retry_delay():
    cache, calls, t = _flaky_cache()
    s, e = dt(2026, 10, 27, 8), dt(2026, 10, 27, 22)
    cache.get(s, e)
    t[0] = 301
    assert cache.get(s, e)[0][2] == "Cours" and len(calls) == 2      # erreur
    t[0] = 330
    assert cache.get(s, e)[0][2] == "Cours" and len(calls) == 2      # dans la fenêtre de retry
    t[0] = 362
    assert cache.get(s, e)[0][2] == "Cours" and len(calls) == 3      # retry écoulé : nouvel appel


def test_agenda_cache_error_without_value_is_held_too():
    calls = []

    def agenda(start, end):
        calls.append(start)
        raise RuntimeError("panne")

    t = [0.0]
    cache = data.AgendaCache(agenda, retry=60, clock=lambda: t[0])
    s, e = dt(2026, 10, 27, 8), dt(2026, 10, 27, 22)
    assert cache.get(s, e) == [] and cache.get(s, e) == [] and len(calls) == 1
    t[0] = 61
    assert cache.get(s, e) == [] and len(calls) == 2
