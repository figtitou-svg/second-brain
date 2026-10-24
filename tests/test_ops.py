"""Alertes de santé, suivi des quotas, date/agenda dans les réponses."""
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import health
import memory
import quota
from bot.fakes import FakeCoach, FakeMemory, FakeRouter
from bot.handlers import BotService, is_question
from coach import store
from coach.calendar_client import FakeCalendarClient
from coach.db import Database
from coach.models import Task
from coach.rules import load_rules
from coach.scheduler import CoachScheduler
from llm_router import router

TZ = ZoneInfo("Europe/Paris")
NOW = datetime(2026, 10, 16, 14, 0, tzinfo=TZ)


# --- santé ---------------------------------------------------------------------------------

def make_health(tmp_path, sent):
    clock = {"t": NOW}
    h = health.Health(lambda t, b: sent.append(t), path=tmp_path / "h.json", now_fn=lambda: clock["t"])
    return h, clock


def test_alert_after_two_failures_once_then_recovery(tmp_path):
    sent = []
    h, clock = make_health(tmp_path, sent)
    h.fail("gmail", RuntimeError("boom"))
    assert sent == []  # un seul échec : pas d'alerte
    h.fail("gmail", RuntimeError("boom"))
    h.fail("gmail", RuntimeError("boom"))
    assert len(sent) == 1 and "Gmail" in sent[0]  # une seule alerte (anti-spam)
    clock["t"] += timedelta(hours=7)
    h.fail("gmail", RuntimeError("boom"))
    assert len(sent) == 2  # rappel après le délai
    h.ok("gmail")
    assert "fonctionne de nouveau" in sent[-1]
    h.ok("gmail")
    assert len(sent) == 3  # pas de second « rétabli »


def test_google_auth_expiry_alerts_immediately(tmp_path):
    sent = []
    h, _ = make_health(tmp_path, sent)
    h.fail("drive", Exception("invalid_grant: Token has been expired or revoked."))
    assert len(sent) == 1 and "Autorisation Google" in sent[0] and "authentification" in sent[0]


def test_guard_never_raises_and_reports(tmp_path):
    sent = []
    h, _ = make_health(tmp_path, sent)

    def boom():
        raise ValueError("x")
    health.guard(h, "notion", boom)()
    health.guard(h, "notion", boom)()
    assert len(sent) == 1 and "Notion" in sent[0]


def test_startup_detects_crash_and_long_gap(tmp_path):
    sent = []
    running, beat = tmp_path / "running", tmp_path / "beat"
    health.startup_check(lambda t, b: sent.append(t), NOW, running, beat)
    assert sent == []  # premier démarrage
    health.startup_check(lambda t, b: sent.append(t), NOW, running, beat)  # running présent = pas d'arrêt propre
    assert len(sent) == 1 and "arrêt inattendu" in sent[0]
    health.clean_exit(running)
    health.beat(beat, NOW - timedelta(minutes=45))
    health.startup_check(lambda t, b: sent.append(t), NOW, running, beat)
    assert len(sent) == 2 and "45 min" in sent[1]
    health.clean_exit(running)
    health.beat(beat, NOW - timedelta(minutes=1))
    health.startup_check(lambda t, b: sent.append(t), NOW, running, beat)
    assert len(sent) == 2  # redémarrage rapide (déploiement) : silence


# --- quota ---------------------------------------------------------------------------------

def write_log(path, lines):
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_quota_report_and_thresholds(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_DAILY_REQUESTS", "10")
    log = tmp_path / "usage.log"
    d = "2026-10-16"
    write_log(log, [f"{d} gemini gemini/x 1"] * 8 + [f"{d} gemini gemini/x err=429"] * 2
              + [f"{d} groq groq/y 1", f"{d} groq groq/y tokens=5000", "2026-10-15 gemini gemini/x 1",
                 f"{d} ollama ollama/q 1"])
    rep = quota.read(log, date(2026, 10, 16))
    assert rep.today["gemini"].requests == 8 and rep.today["gemini"].errors == 2
    assert rep.today["groq"].tokens == 5000 and "ollama" not in rep.today
    text = quota.report(NOW, log)
    assert "8/10" in text and "80%" in text and "🟠" in text and "2 refus" in text
    sent, claimed = [], set()
    claim = lambda k: (k not in claimed) and not claimed.add(k)  # noqa: E731
    quota.check_thresholds(lambda t, b: sent.append(t), claim, NOW, log)
    quota.check_thresholds(lambda t, b: sent.append(t), claim, NOW, log)
    assert len(sent) == 1 and "bientôt épuisé" in sent[0]  # une seule alerte par palier et par jour


def test_quota_empty_log_is_fine(tmp_path):
    assert "Gemini" in quota.report(NOW, tmp_path / "absent.log")


# --- date / agenda -------------------------------------------------------------------------

def make_sched():
    db = Database(":memory:")
    cal = FakeCalendarClient([(datetime(2026, 10, 16, 15, 0, tzinfo=TZ), datetime(2026, 10, 16, 17, 0, tzinfo=TZ))])
    s = CoachScheduler(db, cal, load_rules(), lambda t, b: None, now_fn=lambda: NOW)
    store.configure(db, s)
    return db, s


def test_agenda_context_has_date_agenda_and_deadlines():
    db, s = make_sched()
    db.upsert_task(Task(id="a", title="Rendre TP3", due=NOW + timedelta(days=2), estimated_minutes=60))
    db.upsert_task(Task(id="b", title="Vieux truc", due=NOW - timedelta(days=1)))
    ctx = s.agenda_context(now=NOW)
    assert "vendredi 16 octobre 2026, 14:00" in ctx
    assert "15:00–17:00" in ctx and "Rendre TP3" in ctx and "EN RETARD" in ctx


def test_questions_are_detected():
    for q in ("j'ai quoi cet aprem ?", "Qu'est-ce que j'ai demain", "mon planning de la semaine",
              "c'est quand le partiel ?", "Quel cours jeudi"):
        assert is_question(q), q
    for t in ("Envoyer le devis au BdE demain 1h", "rdv médecin vendredi 15h", "ok"):
        assert not is_question(t), t


def test_question_in_free_text_goes_to_ask_with_agenda_context():
    coach, mem = FakeCoach(), FakeMemory()
    replies = BotService(coach, mem, FakeRouter(), 1).on_text("j'ai quoi cet aprem ?")
    assert mem.questions == ["j'ai quoi cet aprem ?"] and mem.notes == []  # pas enregistré comme note
    assert "Date et heure actuelles" in mem.contexts[0] and replies


def test_memory_ask_sends_date_and_agenda_to_model(monkeypatch, tmp_path):
    monkeypatch.setattr(memory, "DB_PATH", tmp_path / "m.sqlite")
    monkeypatch.setattr(memory, "_conn", None)
    seen = {}

    def fake_complete(prompt, *, sensitivity, system=None, json_mode=False):
        seen.update(prompt=prompt, sens=sensitivity)
        return "Tu as un cours à 15h."
    monkeypatch.setattr(router, "complete", fake_complete)
    monkeypatch.setenv("ALLOW_CLOUD_FOR_NOTES", "true")
    ans = memory.ask("j'ai quoi cet aprem ?", context="Date et heure actuelles : vendredi 16 octobre 2026, 14:00.\n15:00 TP")
    assert "vendredi 16 octobre 2026" in seen["prompt"] and "15:00 TP" in seen["prompt"]
    assert seen["sens"] == "public" and ans.sources == ["Agenda"]


def test_private_sources_skipped_when_local_model_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(memory, "DB_PATH", tmp_path / "m.sqlite")
    monkeypatch.setattr(memory, "_conn", None)
    memory.add_note("code porte garage 4721", "private", "t")
    calls = []

    def fake_complete(prompt, *, sensitivity, system=None, json_mode=False):
        calls.append(sensitivity)
        if sensitivity == "private":
            raise router.PrivateModelUnavailableError("pas d'ollama")
        assert "4721" not in prompt  # la source privée ne part jamais au cloud
        return "Rien de public sur ce sujet."
    monkeypatch.setattr(router, "complete", fake_complete)
    ans = memory.ask("quel est le code porte garage ?", context="Date et heure actuelles : x")
    assert calls == ["private", "public"] and "ignorées" in ans.text
