from __future__ import annotations

from datetime import timedelta

import pytest

from coach.calendar_client import FakeCalendarClient
from coach.db import Database
from coach.models import Task
from coach.rules import load_rules
from coach.scheduler import CoachScheduler
from tests.coach.conftest import FROZEN, dt

NOW = dt(2026, 10, 15, 9, 0)  # jeudi


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def env():
    rules = load_rules(FROZEN)
    db = Database(":memory:")
    cal = FakeCalendarClient([(dt(2026, 10, 15, 18, 30), dt(2026, 10, 15, 21, 30))])
    sent: list[tuple[str, list | None]] = []
    clock = Clock(NOW)
    s = CoachScheduler(db, cal, rules, lambda text, buttons=None: sent.append((text, buttons)), clock)
    return s, db, cal, sent, clock


def mk(title="Rapport", minutes=60, due=None, **kw):
    return Task(title=title, estimated_minutes=minutes, due=due or dt(2026, 10, 20, 18), **kw)


def test_schedule_creates_events_and_blocks(env):
    s, db, cal, sent, _ = env
    t = mk()
    res = s.schedule_tasks([t])
    assert res.unplaced == {}
    blocks = db.list_blocks(t.id)
    assert len(blocks) == 1 and blocks[0].calendar_event_id in cal.events
    assert cal.events[blocks[0].calendar_event_id].summary == "🎯 ⭐ Rapport"  # seule tâche : dans le focus
    assert db.get_task(t.id).status == "scheduled"


def test_two_batches_do_not_overlap(env):
    s, db, cal, *_ = env
    s.schedule_tasks([mk("a", 120)])
    s.schedule_tasks([mk("b", 120)])
    evs = sorted(cal.events.values(), key=lambda e: e.start)
    for x, y in zip(evs, evs[1:]):
        assert x.end + timedelta(minutes=15) <= y.start


def test_urgent_task_bumps_later_due_blocks(env):
    s, db, cal, *_ = env
    s.schedule_tasks([mk("Plein", 60, due=dt(2026, 10, 15, 18))])  # occupe le tout début de journée
    later = mk("IA", 120, due=dt(2026, 10, 20, 18))
    s.schedule_tasks([later])
    first = db.list_blocks(later.id)[0]
    assert first.start.date() == NOW.date()
    urgent = mk("Signer", 60, due=dt(2026, 10, 15, 18))
    res = s.schedule_tasks([urgent])
    assert res.unplaced == {}
    assert db.list_blocks(urgent.id, open_only=True)[0].start.date() == NOW.date()
    moved = db.list_blocks(later.id, open_only=True)
    assert len(moved) == 1 and moved[0].start >= db.list_blocks(urgent.id)[0].end
    assert len(cal.events) == 3


def test_full_day_moves_later_blocks_instead_of_asking(env):
    s, db, cal, sent, _ = env
    fillers = [mk(f"lointain{i}", 60, due=dt(2026, 10, 22, 18)) for i in range(6)]
    for f in fillers:
        s.schedule_tasks([f])
    today = [b for f in fillers for b in db.list_blocks(f.id) if b.start.date() == NOW.date()]
    assert len(today) >= 3  # la journée est bien remplie par des tâches lointaines
    urgent = mk("Signer", 60, due=dt(2026, 10, 15, 18))
    res = s.schedule_tasks([urgent])
    assert res.unplaced == {} and not [m for m, _ in sent if "Impossible" in m]
    assert db.list_blocks(urgent.id, open_only=True)[0].start.date() == NOW.date()
    for f in fillers:  # personne n'est perdu : tous gardent un bloc
        assert db.list_blocks(f.id, open_only=True)


def test_unplaceable_task_asks_user(env):
    s, db, cal, sent, _ = env
    s.schedule_tasks([mk("urgent", 120, due=dt(2026, 10, 15, 10))])
    assert any("Impossible" in m for m, _ in sent)
    assert sent[-1][1][0][0] == "Redécouper"


def test_pre_reminder_once(env):
    s, db, cal, sent, clock = env
    t = mk()
    s.schedule_tasks([t])
    blk = db.list_blocks(t.id)[0]  # 09:00
    s.tick(blk.start - timedelta(minutes=20))
    assert not [m for m, _ in sent if "Dans" in m]
    s.tick(blk.start - timedelta(minutes=14))
    s.tick(blk.start - timedelta(minutes=9))
    pre = [m for m, _ in sent if "Dans 15 min" in m]
    assert len(pre) == 1 and "Rapport" in pre[0]


def test_end_prompt_with_buttons_once(env):
    s, db, cal, sent, _ = env
    t = mk()
    s.schedule_tasks([t])
    blk = db.list_blocks(t.id)[0]
    s.tick(blk.end + timedelta(minutes=1))
    s.tick(blk.end + timedelta(minutes=6))
    prompts = [(m, b) for m, b in sent if "C'est fait" in m]
    assert len(prompts) == 1
    labels = [l for l, _ in prompts[0][1]]
    assert labels == ["Oui", "Pas fini", "Reporter", "Semaine pro."]
    assert prompts[0][1][0][1] == f"done:{blk.id}"


def test_no_prompt_long_after_end(env):
    s, db, cal, sent, _ = env
    t = mk()
    s.schedule_tasks([t])
    blk = db.list_blocks(t.id)[0]
    s.tick(blk.end + timedelta(hours=10))
    assert not any("C'est fait" in m for m, _ in sent)


def test_done_marks_task_done(env):
    s, db, *_ = env
    t = mk()
    s.schedule_tasks([t])
    blk = db.list_blocks(t.id)[0]
    s.handle_callback(f"done:{blk.id}")
    assert db.get_task(t.id).status == "done"
    assert db.get_block(blk.id).outcome == "done"


def test_done_on_split_task_waits_for_all_blocks(env):
    s, db, *_ = env
    t = mk(minutes=300)
    s.schedule_tasks([t])
    blocks = db.list_blocks(t.id)
    assert len(blocks) >= 3
    s.handle_callback(f"done:{blocks[0].id}")
    assert db.get_task(t.id).status == "scheduled"
    for b in blocks[1:]:
        s.handle_callback(f"done:{b.id}")
    assert db.get_task(t.id).status == "done"


def test_not_finished_replans_later(env):
    s, db, cal, sent, clock = env
    t = mk(minutes=60)
    s.schedule_tasks([t])
    blk = db.list_blocks(t.id)[0]
    clock.t = blk.end
    s.handle_callback(f"notfinished:{blk.id}")
    assert db.get_block(blk.id).outcome == "not_finished"
    assert db.get_task(t.id).reschedule_count == 1
    open_blocks = db.list_blocks(t.id, open_only=True)
    assert len(open_blocks) == 1 and open_blocks[0].start >= blk.end
    assert db.get_task(t.id).status == "scheduled"


def test_postpone_moves_existing_event(env):
    s, db, cal, sent, clock = env
    t = mk(minutes=60)
    s.schedule_tasks([t])
    blk = db.list_blocks(t.id)[0]
    clock.t = blk.start - timedelta(minutes=10)
    s.handle_callback(f"postpone:{blk.id}")
    new = db.list_blocks(t.id, open_only=True)[0]
    assert new.calendar_event_id == blk.calendar_event_id
    assert cal.events[new.calendar_event_id].start == new.start
    assert len(cal.events) == 1


def test_postpone_never_same_day_but_next_day(env):
    s, db, cal, sent, clock = env
    t = mk(minutes=60)
    s.schedule_tasks([t])
    blk = db.list_blocks(t.id)[0]  # jeudi 15, 09:00 : sans le report, le créneau suivant serait le jour même
    clock.t = blk.start - timedelta(minutes=10)
    s.handle_callback(f"postpone:{blk.id}")
    new = db.list_blocks(t.id, open_only=True)[0]
    assert new.start.date() == dt(2026, 10, 16).date()


def test_next_week_goes_to_monday_or_later(env):
    s, db, cal, sent, clock = env
    t = mk(minutes=60, due=dt(2026, 10, 30, 18))
    s.schedule_tasks([t])
    blk = db.list_blocks(t.id)[0]
    s.handle_callback(f"nextweek:{blk.id}")
    new = db.list_blocks(t.id, open_only=True)[0]
    assert new.start.date() >= dt(2026, 10, 19).date()  # lundi suivant le jeudi 15
    assert db.get_block(blk.id).outcome == "rescheduled"


def test_postpone_task_from_task_list(env):
    s, db, cal, sent, clock = env
    t = mk(minutes=60, due=dt(2026, 10, 30, 18))
    s.schedule_tasks([t])
    old = db.list_blocks(t.id)[0]
    assert "Reporté" in s.postpone_task(t.id, "next_week")
    (new,) = db.list_blocks(t.id, open_only=True)
    assert new.start.date() >= dt(2026, 10, 19).date()
    assert new.calendar_event_id == old.calendar_event_id and len(cal.events) == 1
    assert db.get_task(t.id).status == "scheduled"


def test_new_calendar_event_displaces_coach_block(env):
    s, db, cal, sent, clock = env
    t = mk(minutes=60)
    s.schedule_tasks([t])
    blk = db.list_blocks(t.id)[0]
    clock.t = blk.start - timedelta(minutes=30)
    cal.external_busy.append((blk.start + timedelta(minutes=10), blk.end + timedelta(minutes=30)))  # ajouté à la main
    s.tick(clock.t)
    (new,) = db.list_blocks(t.id, open_only=True)
    assert new.id != blk.id and not (new.start < cal.external_busy[-1][1] and new.end > cal.external_busy[-1][0])
    assert new.calendar_event_id == blk.calendar_event_id and cal.events[new.calendar_event_id].start == new.start
    assert any("passe avant" in m and "Rapport" in m for m, _ in sent)
    sent.clear()
    s.tick(clock.t)  # plus de conflit : rien de plus
    assert not any("passe avant" in m for m, _ in sent)


def test_started_block_is_not_displaced(env):
    s, db, cal, sent, clock = env
    t = mk(minutes=60)
    s.schedule_tasks([t])
    blk = db.list_blocks(t.id)[0]
    clock.t = blk.start + timedelta(minutes=5)
    cal.external_busy.append((blk.start, blk.end))
    assert s.resolve_conflicts() == []


def test_replan_respects_busy_and_rugby(env):
    s, db, cal, sent, clock = env
    t = mk(minutes=60, due=dt(2026, 10, 16, 12))
    s.schedule_tasks([t])
    for _ in range(2):
        blk = db.list_blocks(t.id, open_only=True)[0]
        clock.t = blk.end
        s.handle_callback(f"notfinished:{blk.id}")
    for b in db.list_blocks(t.id, open_only=True):
        assert not (b.start < dt(2026, 10, 15, 21, 30) and b.end > dt(2026, 10, 15, 18, 30))


def test_third_report_asks_split_or_drop(env):
    s, db, cal, sent, clock = env
    t = mk(minutes=60, due=dt(2026, 10, 30, 12))
    s.schedule_tasks([t])
    for i in range(3):
        blk = db.list_blocks(t.id, open_only=True)[0]
        clock.t = blk.end
        s.handle_callback(f"postpone:{blk.id}")
    assert db.get_task(t.id).reschedule_count == 3
    assert db.list_blocks(t.id, open_only=True) == []
    msg, buttons = sent[-1]
    assert "redécoupe ou on abandonne" in msg
    assert [b[1] for b in buttons] == [f"split:{t.id}", f"drop:{t.id}"]


def test_drop_removes_events(env):
    s, db, cal, *_ = env
    t = mk()
    s.schedule_tasks([t])
    s.handle_callback(f"drop:{t.id}")
    assert db.get_task(t.id).status == "dropped"
    assert cal.events == {} and db.list_blocks(t.id, open_only=True) == []


def test_split_creates_two_half_tasks(env):
    s, db, cal, *_ = env
    t = mk(minutes=120)
    s.schedule_tasks([t])
    s.handle_callback(f"split:{t.id}")
    assert db.get_task(t.id).status == "dropped"
    new = [x for x in db.list_tasks("scheduled")]
    assert len(new) == 2 and all(x.estimated_minutes == 60 and x.reschedule_count == 0 for x in new)
    assert len(cal.events) == 2


def test_briefing_once_at_0745(env):
    s, db, cal, sent, clock = env
    t = mk(due=dt(2026, 10, 16, 18))
    s.schedule_tasks([t])
    s.tick(dt(2026, 10, 15, 7, 30))
    assert not any("Programme" in m for m, _ in sent)
    s.tick(dt(2026, 10, 15, 7, 50))
    s.tick(dt(2026, 10, 15, 7, 55))
    briefs = [m for m, _ in sent if "Programme" in m]
    assert len(briefs) == 1
    assert "Rapport" in briefs[0] and "Échéances sous 48 h" in briefs[0]
    assert "18:30–21:30" in briefs[0]  # entraînement listé dans l'agenda
    s.tick(dt(2026, 10, 16, 7, 46))
    assert len([m for m, _ in sent if "Programme" in m]) == 2


def test_briefing_skipped_if_started_late(env):
    s, db, cal, sent, _ = env
    s.tick(dt(2026, 10, 15, 14, 0))
    assert sent == []


def test_dropped_task_gets_no_reminders(env):
    s, db, cal, sent, _ = env
    t = mk()
    s.schedule_tasks([t])
    blk = db.list_blocks(t.id)[0]
    db.set_task_status(t.id, "dropped")
    s.tick(blk.end + timedelta(minutes=1))
    assert not [m for m, _ in sent if "fait" in m or "Dans" in m]


def test_unknown_callback(env):
    with pytest.raises(ValueError):
        env[0].handle_callback("zzz:1")


def test_postpone_never_same_day_even_past_due(env):
    s, db, cal, *_ = env
    t = mk("Signer", 60, due=dt(2026, 10, 15, 18))
    s.schedule_tasks([t])
    other = mk("Autre", 60, due=dt(2026, 10, 20, 18))
    s.schedule_tasks([other])
    blk = db.list_blocks(t.id)[0]
    s.report(blk.id, "rescheduled")
    new = db.list_blocks(t.id, open_only=True)
    assert new and new[0].start.date() > NOW.date()
    assert db.list_blocks(other.id, open_only=True)[0].start.date() == NOW.date()


def test_replan_all_puts_urgent_first(env):
    s, db, cal, *_ = env
    a = mk("lointain", 60, due=dt(2026, 10, 22, 18))
    b = mk("urgent", 60, due=dt(2026, 10, 15, 18))
    s.schedule_tasks([mk("Plein", 60, due=dt(2026, 10, 15, 18))])  # occupe le tout début de journée
    s.schedule_tasks([a])
    # b est ajoutée "de force" en dessous de a, comme avant la correction
    s.db.upsert_task(b)
    from coach.models import Block
    last = db.list_blocks(a.id)[0]
    nb = Block(task_id=b.id, start=last.end + timedelta(hours=2), end=last.end + timedelta(hours=3))
    nb.calendar_event_id = cal.create_event(nb, b)
    db.add_block(nb)
    db.set_task_status(b.id, "scheduled")
    msg = s.replan_all()
    assert "Planning refait" in msg
    sa, sb = db.list_blocks(a.id, open_only=True)[0], db.list_blocks(b.id, open_only=True)[0]
    assert sb.start < sa.start and len(cal.events) == 3


def test_unanswered_block_is_marked_missed_and_replanned(env):
    s, db, cal, sent, clock = env
    t = mk(minutes=60)
    s.schedule_tasks([t])
    blk = db.list_blocks(t.id)[0]
    clock.t = blk.end + timedelta(hours=7)  # « C'est fait ? » resté sans réponse
    s.tick(clock.t)
    assert db.get_block(blk.id).outcome == "missed"
    nxt = db.list_blocks(t.id, open_only=True)
    assert len(nxt) == 1 and nxt[0].start >= clock.t
    assert nxt[0].calendar_event_id == blk.calendar_event_id  # l'événement est déplacé, pas dupliqué
    assert len(cal.events) == 1
    assert any("Pas de réponse" in m for m, _ in sent)


def test_late_click_on_handled_block_changes_nothing(env):
    s, db, cal, sent, clock = env
    t = mk(minutes=60)
    s.schedule_tasks([t])
    blk = db.list_blocks(t.id)[0]
    clock.t = blk.end + timedelta(hours=7)
    s.tick(clock.t)
    before = [(b.start, b.end) for b in db.list_blocks(t.id, open_only=True)]
    for cb in (f"postpone:{blk.id}", f"notfinished:{blk.id}", f"done:{blk.id}"):
        assert "déjà" in s.handle_callback(cb).lower()
    assert [(b.start, b.end) for b in db.list_blocks(t.id, open_only=True)] == before
    assert db.get_task(t.id).status == "scheduled"


def test_concurrent_scheduling_never_overlaps(env):
    import threading
    import time as _time

    s, db, cal, *_ = env
    real = cal.freebusy

    def slow_freebusy(start, end):  # lecture lente : sans verrou, deux placements voient le même agenda libre
        out = real(start, end)
        _time.sleep(0.05)
        return out

    cal.freebusy = slow_freebusy
    tasks = [mk(f"t{i}", 60) for i in range(2)]
    threads = [threading.Thread(target=s.schedule_tasks, args=([t],)) for t in tasks]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    blocks = sorted((b for t in tasks for b in db.list_blocks(t.id)), key=lambda b: b.start)
    assert len(blocks) == 2 and blocks[0].end <= blocks[1].start
