"""/rdv : rendez-vous forcé (texte ou photo), heure demandée si absente ; toute photo est lue."""
import json
from datetime import datetime
from zoneinfo import ZoneInfo

from bot.fakes import FakeCoach, FakeMemory, FakeRouter
from bot.handlers import BotService
from bot.ports import CoachAdapter
from coach import rdv, store
from coach.calendar_client import FakeCalendarClient
from coach.db import Database
from coach.rules import load_rules
from coach.scheduler import CoachScheduler
from tests.coach.conftest import FROZEN

TZ = ZoneInfo("Europe/Paris")
NOW = datetime(2026, 11, 18, 9, 0, tzinfo=TZ)


def real_env(answer):
    db = Database(":memory:")
    cal = FakeCalendarClient()
    store.configure(db, CoachScheduler(db, cal, load_rules(FROZEN), lambda t, b=None: None, now_fn=lambda: NOW))
    store.configure_rdv(lambda prompt, **k: json.dumps(answer), lambda path, prompt: "Affiche : match jeudi 20h")
    return db, cal, BotService(CoachAdapter(), FakeMemory(), FakeRouter("x"), 1)


def test_rdv_with_time_goes_straight_to_the_agenda():
    db, cal, svc = real_env({"title": "Dentiste", "date": "2026-11-19", "start": "14:30", "end": None, "location": "Lyon"})
    (r,) = svc.rdv("dentiste jeudi 14h30 à Lyon")
    assert "Ajouté à l'agenda" in r.text
    (ev,) = db.list_events("added")
    assert ev.start == datetime(2026, 11, 19, 14, 30, tzinfo=TZ) and ev.calendar_event_id in cal.events


def test_rdv_without_time_asks_then_buttons_or_typed_hour():
    db, cal, svc = real_env({"title": "Réunion CHAPS", "date": "2026-11-20", "start": None})
    (r,) = svc.rdv("réunion chaps vendredi")
    assert "À quelle heure" in r.text and r.buttons[0][0][1].startswith("rdvh:")
    assert db.list_events("added") == []
    (r,) = svc.on_text("16h30")                                     # réponse tapée
    assert "Ajouté à l'agenda" in r.text
    assert db.list_events("added")[0].start == datetime(2026, 11, 20, 16, 30, tzinfo=TZ)


def test_rdv_without_date_says_so_and_help_without_text():
    _, _, svc = real_env({"title": "Dentiste", "date": None})
    assert "pas trouvé de date" in svc.rdv("dentiste")[0].text
    assert "/rdv dentiste jeudi" in svc.rdv("")[0].text


def test_photo_with_rdv_caption_forces_the_event(tmp_path):
    coach = FakeCoach()
    coach.photo_text = "Affiche : réunion vendredi"
    svc = BotService(coach, FakeMemory(), FakeRouter("x"), 1)
    out = svc.on_document(str(tmp_path / "p.jpg"), "photo.jpg", "/rdv", "photo")
    assert out[0].text.startswith("🕐") or "Ajouté" in out[0].text
    assert ("add_rdv", "Affiche : réunion vendredi", coach.calls[-1][2]) == coach.calls[-1]


def test_every_photo_is_read_for_events_and_tasks(tmp_path):
    from coach.models import Event
    coach = FakeCoach()
    coach.photo_text = "Match de rugby jeudi 20h au stade Chapuis"
    coach.events = [Event(id="e1", title="Match de rugby", start=NOW, end=NOW.replace(hour=10))]
    svc = BotService(coach, FakeMemory(), FakeRouter("x"), 1)
    out = svc.on_document(str(tmp_path / "p.jpg"), "photo.jpg", None, "photo")
    assert any("Rendez-vous sur la photo" in r.text and r.buttons for r in out)


def test_hour_buttons_shape():
    rows = rdv.hour_buttons("ev")
    assert rows[0][0] == ("08h", "rdvh:ev:0800") and rows[-1][-1] == ("🗑 Annuler", "evign:ev")
