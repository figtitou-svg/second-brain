"""Les vraies règles (coach/rules.yaml) : plafond, heure limite et repas du midi."""
from datetime import time, timedelta

from coach.models import Task
from coach.planner import plan
from coach.rules import load_rules
from tests.coach.conftest import dt

NOW = dt(2026, 10, 19, 7, 0)  # lundi


def blocks_for(n_tasks: int, minutes: int = 60):
    tasks = [Task(id=f"t{i}", title=f"T{i}", estimated_minutes=minutes, due=NOW + timedelta(days=14))
             for i in range(n_tasks)]
    return plan(tasks, [], load_rules(), NOW)


def test_daily_cap_is_above_three_hours_and_respected():
    rules = load_rules()
    assert rules.max_focus_hours_per_day > 3
    per_day: dict = {}
    for b in blocks_for(20):
        per_day[b.start.date()] = per_day.get(b.start.date(), 0) + b.minutes
    assert max(per_day.values()) > 180  # on utilise bien le nouveau plafond
    assert max(per_day.values()) <= rules.max_focus_minutes_per_day


def test_nothing_after_22h_and_lunch_hour_free_every_day():
    blocks = blocks_for(40)
    assert blocks
    for b in blocks:
        assert b.end.time() <= time(22, 0) and b.end.date() == b.start.date()
        # aucun bloc ne touche l'heure du repas 12:30-13:30
        lunch_s = b.start.replace(hour=12, minute=30)
        lunch_e = b.start.replace(hour=13, minute=30)
        assert not (b.start < lunch_e and b.end > lunch_s), (b.start, b.end)


def test_proactive_reminders_stop_before_the_late_evening():
    from datetime import date
    rules = load_rules()
    assert rules.reminder_window(date(2026, 10, 22)).end.hour * 60 + rules.reminder_window(date(2026, 10, 22)).end.minute <= 20 * 60 + 30
    assert rules.reminder_window(date(2026, 10, 22)).end < rules.allowed_hours.weekday.end
