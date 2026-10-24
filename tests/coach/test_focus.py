from __future__ import annotations

import pytest

from coach import focus
from coach.models import Task
from tests.coach.conftest import dt

NOW = dt(2026, 10, 15, 9, 0)  # jeudi
CAP = 180  # minutes de focus par jour (règles figées des tests)
D = dt(2026, 10, 25, 18)


def T(title="t", minutes=60, due=None, **kw) -> Task:
    return Task(title=title, estimated_minutes=minutes, due=due, **kw)


def rk(tasks, done=None, weights=None, now=NOW):
    return focus.rank_tasks(tasks, done or {}, now, weights or {}, CAP)


def by_title(ranked):
    return {r.task.title: r for r in ranked}


def test_tighter_margin_ranks_first():
    soon, later = T("soon", due=dt(2026, 10, 16, 18)), T("later", due=dt(2026, 10, 25, 18))
    assert [r.task.title for r in rk([later, soon])] == ["soon", "later"]


def test_overdue_beats_a_deadline_today():
    late, today = T("late", due=dt(2026, 10, 14, 18)), T("today", due=dt(2026, 10, 15, 20))
    assert rk([today, late])[0].task.title == "late"


def test_reschedules_add_traine_capped_at_30():
    a = T("a", due=D)
    b = T("b", due=D, reschedule_count=2)
    c = T("c", due=D, reschedule_count=9)
    r = by_title(rk([a, b, c]))
    assert r["b"].score - r["a"].score == pytest.approx(20)
    assert r["c"].score - r["a"].score == pytest.approx(30)


def test_subject_weight_scales_and_is_clamped():
    alg, bd = T("[Alg] TP", due=D), T("[BD] TP", due=D)
    r = by_title(rk([alg, bd], weights={"Alg": 2.0, "BD": 0.5}))
    assert r["[Alg] TP"].score == pytest.approx(r["[BD] TP"].score * 4)
    wild = by_title(rk([alg], weights={"Alg": 9.0}))  # borné à 2,0
    assert wild["[Alg] TP"].score == pytest.approx(r["[Alg] TP"].score)


def test_boost_shifts_the_score():
    base, up, down = T("a", due=D), T("b", due=D, boost=2), T("c", due=D, boost=-1)
    r = by_title(rk([base, up, down]))
    assert r["b"].score - r["a"].score == pytest.approx(50)
    assert r["c"].score - r["a"].score == pytest.approx(-25)


def test_task_on_a_given_day_is_forced_that_day_only():
    shoes = T("chaussures", minutes=10, due=dt(2026, 10, 16, 23, 59), not_before=dt(2026, 10, 16))
    urgent = T("urgent", due=dt(2026, 10, 15, 20))
    assert rk([shoes, urgent], now=NOW)[0].task.title == "urgent"  # la veille : pas forcée
    assert rk([shoes, urgent], now=dt(2026, 10, 16, 8))[0].task.title == "chaussures"


def test_focus_is_the_top_three():
    out = rk([T(f"t{i}", due=dt(2026, 10, 16 + i, 18)) for i in range(6)])
    assert [r.level for r in out[:3]] == ["focus"] * 3
    assert all(r.level in ("haut", "moyen", "faible") for r in out[3:])
    assert [r.rank for r in out] == list(range(6))


def test_levels_outside_focus_follow_the_score():
    in_focus = [T(f"f{i}", due=dt(2026, 10, 14, 18)) for i in range(3)]  # en retard
    haut = T("haut", due=dt(2026, 10, 16, 18))
    moyen = T("moyen", due=dt(2026, 10, 18, 18))
    faible = T("faible", due=dt(2026, 11, 15, 18))
    r = by_title(rk(in_focus + [haut, moyen, faible]))
    assert (r["haut"].level, r["moyen"].level, r["faible"].level) == ("haut", "moyen", "faible")


def test_order_is_stable_on_ties():
    a, b = T("b", due=D), T("a", due=D)
    assert [r.task.title for r in rk([a, b])] == ["a", "b"]
    assert [r.task.title for r in rk([b, a])] == ["a", "b"]


def test_task_without_due_is_low_but_positive():
    r = rk([T("x")])[0]
    assert 0 < r.score < focus.LEVEL_MID


def test_done_minutes_reduce_the_remaining_work():
    big = T("big", minutes=600, due=dt(2026, 10, 17, 18))
    assert rk([big], done={big.id: 500})[0].score < rk([big])[0].score


def test_empty_list():
    assert rk([]) == []


def test_adjusted_respects_bounds():
    assert focus.adjusted(2, 2.0, True) == (2, 2.0)
    assert focus.adjusted(-1, 0.5, False) == (-1, 0.5)
    assert focus.adjusted(0, 1.0, True) == (1, 1.1)
    assert focus.adjusted(0, 1.0, False) == (-1, 0.9)


def test_subject_of():
    assert focus.subject_of("[Alg] TP3") == "Alg"
    assert focus.subject_of("Chaussures") is None
