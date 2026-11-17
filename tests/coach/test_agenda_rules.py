"""Agenda : cours jamais suivis masqués (et heures libres), rugby imposé chaque semaine, couleur par type."""
from datetime import datetime

from coach import agenda_rules as ar
from coach.calendar_client import GoogleCalendarClient

TZ = ar.TZ


def test_hidden_courses():
    for t in ("POO2 · CM", "AO · CM", "SPE · TD", "POO1 · TA", "Architecture des circuits · TA", "LV1", "LV2"):
        assert ar.is_hidden(t), t
    for t in ("POO1 · TP", "ANG-3 · TD", "ESP · TD", "Algorithmes · Évaluation", "EPS", "Prise de sang", "BDR · TD"):
        assert not ar.is_hidden(t), t


def test_kind_and_colors():
    assert [ar.kind_of(t) for t in ("Programmation en C · Évaluation", "BDR · TP", "CMSI · TD", "ANG-3 · TD",
                                    "EPS", "Rugby", "Prise de sang", "SHC1-HU · TD")] == \
        ["eval", "tp", "td", "lang", "sport", "sport", "perso", "td"]
    assert ar.color_of("Rugby") == ar.COLORS["sport"]


def test_rugby_every_week():
    got = ar.recurring_between(datetime(2026, 11, 16, tzinfo=TZ), datetime(2026, 11, 23, tzinfo=TZ))
    assert [(s.strftime("%a %H:%M"), e.strftime("%H:%M"), t) for s, e, t in got] == \
        [("Wed 18:00", "20:00", "Rugby"), ("Thu 14:00", "16:00", "Rugby")]


class _Exec:
    def __init__(self, v):
        self.v = v

    def execute(self):
        return self.v


class _Svc:
    """Google factice : un calendrier principal (un rendez-vous) et l'emploi du temps importé."""
    IMP = "abc@import.calendar.google.com"

    def calendarList(self):
        return self

    def list(self, **k):
        if "calendarId" not in k:
            return _Exec({"items": [{"id": "primary"}, {"id": self.IMP}]})
        if k["calendarId"] != self.IMP:
            return _Exec({"items": []})
        ev = lambda t, h1, h2: {"summary": t, "start": {"dateTime": f"2026-11-18T{h1}:00+01:00"},
                                "end": {"dateTime": f"2026-11-18T{h2}:00+01:00"}}
        return _Exec({"items": [ev("POO2 · CM", "08", "10"), ev("BDR · TP", "14", "16"), ev("SPE · TD", "18", "20")]})

    def events(self):
        return self

    def freebusy(self):
        return self

    def query(self, body):
        return _Exec({"calendars": {"primary": {"busy": [{"start": "2026-11-18T11:00:00+01:00",
                                                           "end": "2026-11-18T12:00:00+01:00"}]},
                                    self.IMP: {"busy": [{"start": "2026-11-18T08:00:00+01:00",
                                                         "end": "2026-11-18T20:00:00+01:00"}]}}})


def test_google_client_hides_courses_and_adds_rugby():
    c = GoogleCalendarClient(_Svc(), "primary")
    s, e = datetime(2026, 11, 18, tzinfo=TZ), datetime(2026, 11, 19, tzinfo=TZ)
    assert [t for _, _, t in c.agenda(s, e)] == ["BDR · TP", "Rugby"]
    busy = sorted((a.hour, b.hour) for a, b in c.freebusy(s, e))
    assert busy == [(11, 12), (14, 16), (18, 20)]          # CM du matin libre ; SPE remplacé par le rugby
