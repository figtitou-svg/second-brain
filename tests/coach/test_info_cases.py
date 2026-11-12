"""Infos en cases : une seule info par mail, pas de doublon, une notification par case, les newsletters et les
infos jugées « pas utiles » apprennent au bot à se taire."""
import json
from datetime import date

import pytest

import ingest_gmail
import memory
from bot.fakes import FakeCoach, FakeMemory, FakeRouter
from bot.handlers import BotService
from coach import extract, store
from coach import infos as cases
from coach.calendar_client import FakeCalendarClient
from coach.db import Database
from coach.models import Info
from coach.rules import load_rules
from coach.scheduler import CoachScheduler
from tests.coach.conftest import FROZEN, dt

NOW = dt(2026, 11, 12, 9, 0)


def info(title, cat="cours", imp=2, day=None, iid=None):
    return Info(id=iid or title, title=title, category=cat, importance=imp, day=day)


def test_one_info_per_mail_most_important_and_no_repeat():
    known = [info("DS d'informatique jeudi : règles de l'examen", day=date(2026, 11, 19))]
    new = [info("Salle 207 pour le DS", imp=2), info("DS d'info jeudi : règles de l'examen", imp=3)]
    assert cases.keep(new, []) == [new[1]]                       # une seule, la plus importante
    assert cases.keep([new[1]], known) == []                       # déjà reçue par un autre mail


def test_one_notification_per_case_and_never_for_trivia():
    a, b = info("TD annulé", imp=3), info("Note du QCM en ligne", imp=2)
    c = info("Nouvelle asso de jeux", cat="campus", imp=1)
    assert cases.to_notify([b, a, c]) == [(a, 1)]
    assert "+1 autre dans l'onglet Infos" in cases.message(a, 1)


def test_category_falls_back_to_perso():
    assert cases.category("COURS") == "cours" and cases.category("bidule") == "perso" and cases.category(None) == "perso"


@pytest.fixture
def env(monkeypatch, tmp_path):
    db = Database(":memory:")
    store.configure(db, CoachScheduler(db, FakeCalendarClient(), load_rules(FROZEN), lambda t, b: None, now_fn=lambda: NOW))
    prompts = []
    answer = {"tasks": [], "events": [], "infos": [{"title": "Missive de l'asso", "detail": "Suivez-nous.",
                                                    "category": "campus", "importance": 2}]}

    def llm(prompt, **k):
        prompts.append(prompt)
        return json.dumps(answer)
    monkeypatch.setattr(extract, "_resolve_llm", lambda: llm)
    monkeypatch.setattr(memory, "DB_PATH", tmp_path / "m.sqlite")
    monkeypatch.setattr(memory, "_conn", None)
    monkeypatch.delenv("INFO_SKIP_SENDERS", raising=False)
    return db, prompts, answer


def test_newsletter_sender_gives_no_info(env):
    db, _, _ = env
    store.extract_items("De : Com <news@insa-communication.insa-lyon.fr>\nLettre unique 81", "insa:mid:81", "private")
    assert db.list_infos() == []


def test_not_useful_hides_teaches_and_mutes_the_sender(env):
    db, prompts, answer = env
    sent = []
    ingest_gmail.process_mail("De : Asso <va@asso.fr>\nMissive numéro 1, test pas utile", "Missive", "gmail:1",
                              lambda t, b: sent.append(b))
    (first,) = db.list_infos()
    reply = BotService(store_coach(), FakeMemory(), FakeRouter("x"), 1).on_callback(f"inon:{first.id}")
    assert "Noté" in reply.text and db.list_infos() == [] and db.unseen_infos() == 0
    answer["infos"][0]["title"] = "Résultats du tournoi de l'asso"
    store.extract_items("De : Asso <va@asso.fr>\nMissive numéro 2, test pas utile", "gmail:2", "private")
    assert "PAS UTILES" in prompts[-1] and "Missive de l'asso" in prompts[-1]   # contre-exemple
    (second,) = db.list_infos()
    assert "ne donnent plus d'info" in store.dismiss_info(second.id)
    answer["infos"][0]["title"] = "Soirée de l'asso vendredi"
    store.extract_items("De : Asso <va@asso.fr>\nMissive numéro 3, test pas utile", "gmail:3", "private")
    assert db.list_infos() == []                                              # expéditeur devenu muet


def store_coach():
    from bot.ports import CoachAdapter
    return CoachAdapter()


def test_fake_coach_records_dismiss():
    coach = FakeCoach()
    assert BotService(coach, FakeMemory(), FakeRouter("x"), 1).on_callback("inon:abc").text.startswith("👎")
    assert ("dismiss_info", "abc") in coach.calls
