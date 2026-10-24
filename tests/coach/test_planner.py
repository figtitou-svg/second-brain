from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from coach.models import Task
from coach.planner import plan, plan_detailed
from coach.rules import Rules, load_rules
from tests.coach.conftest import FROZEN, PARIS, dt

# 2026-10-15 = jeudi ; 17 = samedi ; 18 = dimanche ; 19 = lundi
NOW = dt(2026, 10, 15, 9, 0)


def T(title="t", minutes=60, due=None, prio="medium", **kw) -> Task:
    return Task(title=title, estimated_minutes=minutes, due=due, priority=prio, **kw)


def assert_invariants(blocks, busy, rules: Rules, tasks):
    buf = timedelta(minutes=rules.buffer_minutes)
    bl = sorted(blocks, key=lambda b: b.start)
    for b in blocks:
        assert b.end > b.start
        assert (b.end - b.start) <= timedelta(minutes=rules.max_block_minutes)
        for s, e in busy:
            assert b.end + buf <= s or b.start - buf >= e, f"overlap {b} / {(s, e)}"
        loc_s, loc_e = b.start.astimezone(PARIS), b.end.astimezone(PARIS)
        assert loc_s.date() == loc_e.date()
        h = rules.allowed_hours.weekend if loc_s.weekday() >= 5 else rules.allowed_hours.weekday
        assert loc_s.time() >= h.start and loc_e.time() <= h.end
        for bs in rules.blocked:
            if ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday",
                "sunday"].index(bs.day) == loc_s.weekday():
                assert loc_e.time() <= bs.from_ or loc_s.time() >= bs.to
    for a, b in zip(bl, bl[1:]):
        assert a.end + buf <= b.start
    per_day: dict = {}
    for b in blocks:
        d = b.start.astimezone(PARIS).date()
        per_day[d] = per_day.get(d, 0) + b.minutes
    assert all(v <= rules.max_focus_minutes_per_day for v in per_day.values())
    due = {t.id: t.due for t in tasks}
    for b in blocks:
        if due.get(b.task_id):
            assert b.end <= due[b.task_id]


def test_earliest_placement(rules):
    t = T(minutes=60, due=dt(2026, 10, 20, 18))
    blocks = plan([t], [], rules, NOW)
    assert len(blocks) == 1
    assert blocks[0].start == dt(2026, 10, 15, 9, 0)
    assert blocks[0].end == dt(2026, 10, 15, 10, 0)
    assert blocks[0].task_id == t.id


def test_now_rounded_up_to_5_minutes(rules):
    blocks = plan([T(minutes=30)], [], rules, dt(2026, 10, 15, 9, 1))
    assert blocks[0].start == dt(2026, 10, 15, 9, 5)


def test_before_allowed_hours_starts_at_opening(rules):
    blocks = plan([T(minutes=30)], [], rules, dt(2026, 10, 15, 5, 0))
    assert blocks[0].start == dt(2026, 10, 15, 8, 0)


def test_after_allowed_hours_goes_next_day(rules):
    blocks = plan([T(minutes=30)], [], rules, dt(2026, 10, 15, 22, 30))
    assert blocks[0].start == dt(2026, 10, 16, 8, 0)


def test_avoids_busy_with_buffer(rules):
    busy = [(dt(2026, 10, 15, 9, 0), dt(2026, 10, 15, 12, 0))]
    blocks = plan([T(minutes=60)], busy, rules, NOW)
    assert blocks[0].start == dt(2026, 10, 15, 12, 15)
    assert_invariants(blocks, busy, rules, [])


def test_gap_too_small_is_skipped(rules):
    busy = [
        (dt(2026, 10, 15, 8, 0), dt(2026, 10, 15, 9, 0)),
        (dt(2026, 10, 15, 9, 40), dt(2026, 10, 15, 12, 0)),
    ]
    blocks = plan([T(minutes=60)], busy, rules, NOW)
    assert blocks[0].start >= dt(2026, 10, 15, 12, 15)


def test_rugby_blocked_thursday(rules):
    t = T(minutes=150, due=dt(2026, 10, 15, 23, 0))
    res = plan_detailed([t], [], rules, dt(2026, 10, 15, 17, 0))
    for b in res.blocks:
        assert b.end <= dt(2026, 10, 15, 18, 15) or b.start >= dt(2026, 10, 15, 21, 45)
    assert sum(b.minutes for b in res.blocks) + sum(res.unplaced.values()) == 150
    assert_invariants(res.blocks, [], rules, [t])


def test_rugby_tuesday(rules):
    now = dt(2026, 10, 20, 17, 0)  # mardi
    blocks = plan([T(minutes=90)], [], rules, now)
    assert_invariants(blocks, [], rules, [])
    for b in blocks:
        assert b.end <= dt(2026, 10, 20, 18, 15) or b.start >= dt(2026, 10, 20, 21, 45)


def test_split_over_max_block(rules):
    t = T(minutes=300, due=dt(2026, 10, 20, 18))
    blocks = plan([t], [], rules, NOW)
    assert all(b.minutes <= 120 for b in blocks)
    assert sum(b.minutes for b in blocks) == 300
    assert len(blocks) >= 3
    assert_invariants(blocks, [], rules, [t])


def test_no_tiny_remainder_when_splitting(rules):
    t = T(minutes=130, due=dt(2026, 10, 25, 18))
    blocks = plan([t], [], rules, NOW)
    assert sum(b.minutes for b in blocks) == 130
    assert min(b.minutes for b in blocks) >= rules.min_block_minutes


def test_daily_focus_cap(rules):
    t = T(minutes=480, due=dt(2026, 10, 30, 18))
    blocks = plan([t], [], rules, NOW)
    assert sum(b.minutes for b in blocks) == 480
    assert_invariants(blocks, [], rules, [t])
    days = {b.start.astimezone(PARIS).date() for b in blocks}
    assert len(days) >= 3


def test_cap_shared_between_tasks(rules):
    tasks = [T("a", 120, prio="high"), T("b", 120, prio="low")]
    blocks = plan(tasks, [], rules, NOW)
    day1 = [b for b in blocks if b.start.date() == NOW.date()]
    assert sum(b.minutes for b in day1) <= 180
    assert sum(b.minutes for b in blocks) == 240


def test_priority_order(rules):
    lo = T("lo", 60, prio="low")
    hi = T("hi", 60, prio="high")
    blocks = plan([lo, hi], [], rules, NOW)
    assert blocks[0].task_id == hi.id


def test_same_priority_earlier_due_first(rules):
    late = T("late", 60, due=dt(2026, 10, 25, 18))
    soon = T("soon", 60, due=dt(2026, 10, 16, 18))
    blocks = plan([late, soon], [], rules, NOW)
    assert blocks[0].task_id == soon.id


def test_no_overlap_between_tasks(rules):
    tasks = [T(f"t{i}", 90, prio="high") for i in range(5)]
    blocks = plan(tasks, [], rules, NOW)
    assert_invariants(blocks, [], rules, tasks)


def test_weekend_hours(rules):
    now = dt(2026, 10, 17, 7, 0)  # samedi
    blocks = plan([T(minutes=60)], [], rules, now)
    assert blocks[0].start == dt(2026, 10, 17, 10, 0)
    late = plan([T(minutes=60)], [], rules, dt(2026, 10, 17, 19, 30))
    assert_invariants(late, [], rules, [])


def test_friday_evening_to_weekend(rules):
    blocks = plan([T(minutes=30)], [], rules, dt(2026, 10, 16, 22, 10))
    assert blocks[0].start == dt(2026, 10, 17, 10, 0)


def test_due_too_close_partial_and_reported(rules):
    t = T(minutes=120, due=dt(2026, 10, 15, 10, 0))
    res = plan_detailed([t], [], rules, NOW)
    assert sum(b.minutes for b in res.blocks) == 60
    assert res.unplaced == {t.id: 60}
    assert all(b.end <= t.due for b in res.blocks)


def test_due_in_past_nothing_placed(rules):
    t = T(minutes=30, due=dt(2026, 10, 14, 10, 0))
    res = plan_detailed([t], [], rules, NOW)
    assert res.blocks == []
    assert res.unplaced == {t.id: 30}


def test_due_exactly_fits(rules):
    t = T(minutes=60, due=dt(2026, 10, 15, 10, 0))
    res = plan_detailed([t], [], rules, NOW)
    assert res.unplaced == {}
    assert res.blocks[0].end == t.due


def test_due_none_defaults_to_7_days(rules):
    busy = [(dt(2026, 10, 15, 0), dt(2026, 10, 22, 9, 0))]
    res = plan_detailed([T(minutes=60)], busy, rules, NOW)
    assert res.blocks == []


def test_done_and_dropped_ignored(rules):
    ts = [T(status="done"), T(status="dropped")]
    assert plan(ts, [], rules, NOW) == []


def test_scheduled_tasks_are_replanned(rules):
    assert len(plan([T(status="scheduled")], [], rules, NOW)) == 1


def test_naive_datetime_rejected(rules):
    with pytest.raises(ValueError):
        plan([T()], [], rules, datetime(2026, 10, 15, 9, 0))
    with pytest.raises(ValueError):
        plan([T()], [(datetime(2026, 10, 15, 9), datetime(2026, 10, 15, 10))], rules, NOW)


def test_utc_inputs_converted(rules):
    blocks = plan([T(minutes=30)], [], rules, NOW.astimezone(timezone.utc))
    assert blocks[0].start == NOW


def test_no_tasks(rules):
    assert plan([], [], rules, NOW) == []


def test_deterministic_and_pure(rules):
    tasks = [T("a", 200), T("b", 90, prio="high")]
    busy = [(dt(2026, 10, 16, 10), dt(2026, 10, 16, 12))]
    a = plan(tasks, busy, rules, NOW)
    b = plan(tasks, busy, rules, NOW)
    assert [(x.task_id, x.start, x.end) for x in a] == [(x.task_id, x.start, x.end) for x in b]
    assert all(t.status == "pending" for t in tasks)


def test_dst_change_day(rules):
    now = dt(2026, 10, 24, 20, 0)  # passage à l'heure d'hiver le 25
    t = T(minutes=60)
    blocks = plan([t], [], rules, now)
    assert_invariants(blocks, [], rules, [t])


def test_realistic_week(rules):
    busy = []
    for day in range(15, 21):
        busy += [(dt(2026, 10, day, 8, 0), dt(2026, 10, day, 12, 0)),
                 (dt(2026, 10, day, 13, 30), dt(2026, 10, day, 17, 30))]
    tasks = [T("rapport", 240, due=dt(2026, 10, 21, 12), prio="high"),
             T("devis", 60, due=dt(2026, 10, 17, 18), prio="high"),
             T("lecture", 90, prio="low")]
    res = plan_detailed(tasks, busy, rules, NOW)
    assert_invariants(res.blocks, busy, rules, tasks)
    assert res.unplaced == {}


def test_custom_rules_no_buffer():
    r = Rules.model_validate({
        "allowed_hours": {"weekday": ["08:00", "12:00"], "weekend": ["10:00", "12:00"]},
        "buffer_minutes": 0, "max_block_minutes": 60, "max_focus_hours_per_day": 10,
    })
    blocks = plan([T(minutes=120, due=dt(2026, 10, 16, 12))], [], r, NOW)
    assert (blocks[0].start, blocks[0].end) == (dt(2026, 10, 15, 9), dt(2026, 10, 15, 10))
    assert sum(b.minutes for b in blocks) == 120
    assert_invariants(blocks, [], r, [])


def test_rules_loader_defaults():
    r = load_rules(FROZEN)
    assert r.timezone == "Europe/Paris"
    assert r.buffer_minutes == 15 and r.max_block_minutes == 120
    assert r.max_focus_minutes_per_day == 180
    assert {b.day for b in r.blocked} == {"tuesday", "wednesday", "thursday"}
    assert r.allowed_hours.weekday.start.hour == 8


def test_one_hour_task_prefers_a_single_block_over_splitting(rules):
    # vendredi 16 : 8h-9h libre 55 min seulement (occupé dès 9h), puis libre à partir de 10h30
    busy = [(dt(2026, 10, 16, 8, 55), dt(2026, 10, 16, 10, 30))]
    t = T(minutes=60, due=dt(2026, 10, 17, 18))
    blocks = plan([t], busy, rules, dt(2026, 10, 16, 7, 0))
    assert len(blocks) == 1 and blocks[0].minutes == 60


def test_split_when_no_single_slot_but_never_a_tiny_leftover(rules):
    busy = [(dt(2026, 10, 16, 9, 55), dt(2026, 10, 16, 22, 0))]  # une seule plage de ~55 min ce jour-là
    t = T(minutes=60, due=dt(2026, 10, 16, 22))
    blocks = plan([t], busy, rules, dt(2026, 10, 16, 7, 0))
    assert all(b.minutes >= rules.min_block_minutes for b in blocks)


def test_task_with_start_day_waits_for_that_day(rules):
    t = T(minutes=30, due=dt(2026, 10, 19, 23, 59), not_before=dt(2026, 10, 19))
    blocks = plan([t], [], rules, NOW)
    assert len(blocks) == 1 and blocks[0].start.date().isoformat() == "2026-10-19"


# --- démarrage selon la complexité (start_lead_days) -------------------------------------------

LEADS = [{"up_to_minutes": 15, "days": 0}, {"up_to_minutes": 60, "days": 2},
         {"up_to_minutes": 120, "days": 4}, {"days": 7}]


@pytest.fixture
def lead_rules(rules):
    return Rules.model_validate({**rules.model_dump(by_alias=True), "start_lead_days": LEADS})


def test_lead_days_by_duration(lead_rules):
    assert [lead_rules.lead_days(m) for m in (5, 15, 16, 60, 61, 120, 121, 600)] == [0, 0, 2, 2, 4, 4, 7, 7]


def test_lead_days_without_tiers_is_none(rules):
    assert rules.lead_days(600) is None


def test_short_task_waits_for_due_day(lead_rules):
    t = T("chaussures", minutes=5, due=dt(2026, 10, 20, 18))
    (b,) = plan([t], [], lead_rules, NOW)
    assert b.start == dt(2026, 10, 20, 8, 0)


def test_medium_task_starts_two_days_before(lead_rules):
    t = T("formulaire", minutes=45, due=dt(2026, 10, 20, 18))
    (b,) = plan([t], [], lead_rules, NOW)
    assert b.start == dt(2026, 10, 18, 10, 0)  # dimanche : ouverture à 10h


def test_big_task_starts_a_week_before(lead_rules):
    t = T("dossier", minutes=180, due=dt(2026, 10, 30, 18))
    blocks = plan([t], [], lead_rules, NOW)
    assert min(b.start for b in blocks) >= dt(2026, 10, 23, 0)


def test_no_due_is_placed_asap(lead_rules):
    (b,) = plan([T(minutes=5)], [], lead_rules, NOW)
    assert b.start == dt(2026, 10, 15, 9, 0)


def test_lead_never_before_now(lead_rules):
    t = T(minutes=45, due=dt(2026, 10, 16, 18))  # échéance demain : fenêtre déjà commencée
    (b,) = plan([t], [], lead_rules, NOW)
    assert b.start == dt(2026, 10, 15, 9, 0)


def test_not_before_later_than_lead_wins(lead_rules):
    t = T(minutes=45, due=dt(2026, 10, 25, 18), not_before=dt(2026, 10, 24))
    (b,) = plan([t], [], lead_rules, NOW)
    assert b.start >= dt(2026, 10, 24, 0)


def test_falls_back_to_early_start_when_short_window_too_small(lead_rules):
    # le jour de l'échéance est entièrement occupé : la fenêtre « jour même » ne contient aucun créneau
    busy = [(dt(2026, 10, 20, 8), dt(2026, 10, 20, 22))]
    t = T("pas de place", minutes=15, due=dt(2026, 10, 20, 18))
    res = plan_detailed([t], busy, lead_rules, NOW)
    assert not res.unplaced
    assert res.blocks[0].start.date() < dt(2026, 10, 20).date()
