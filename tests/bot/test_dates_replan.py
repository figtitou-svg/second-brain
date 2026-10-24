from datetime import date

from bot.dates import parse_date
from bot.fakes import FakeCoach, FakeTask, FakeMemory, FakeRouter
from bot.handlers import BotService

TODAY = date(2026, 10, 19)


def test_parse_dates():
    assert parse_date("4 janvier", TODAY) == date(2027, 1, 4)
    assert parse_date("janvier", TODAY) == date(2027, 1, 1)
    assert parse_date("le 15/01", TODAY) == date(2027, 1, 15)
    assert parse_date("1er décembre", TODAY) == date(2026, 12, 1)
    assert parse_date("demain", TODAY) == date(2026, 10, 20)
    assert parse_date("lundi", TODAY) == date(2026, 10, 26)
    assert parse_date("n'importe quoi", TODAY) is None
    assert parse_date("31 février", TODAY) is None


def _svc():
    coach = FakeCoach()
    return BotService(coach, FakeMemory(), FakeRouter(), 1), coach


def test_replan_command():
    svc, coach = _svc()
    assert svc.replan()[0].text == "Planning refait"
    assert ("replan_all",) in coach.calls


def test_reporte_a_date_libre(monkeypatch):
    svc, coach = _svc()
    t = coach.tasks["t1"] = FakeTask("t1", "Envoyer un mail à Ines")
    r = svc.postpone_to("mail à ines", "4 janvier", TODAY)
    assert r and ("postpone_task", str(t.id), "2027-01-04") in coach.calls


def test_reporte_date_illisible_laisse_la_note():
    svc, _ = _svc()
    assert svc.postpone_to("x", "bientôt", TODAY) is None
