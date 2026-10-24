from __future__ import annotations

import asyncio
from types import SimpleNamespace

from bot.fakes import FakeCoach, FakeMemory, FakeRouter, FakeTask
from bot.handlers import COMMANDS, BotService, TelegramHandlers
from bot.ports import NotReadyError

ALLOWED = 42


def make(tasks=None):
    coach = FakeCoach(tasks or [])
    mem = FakeMemory()
    return BotService(coach, mem, FakeRouter(), ALLOWED), coach, mem


class Msg:
    def __init__(self):
        self.sent = []

    async def reply_text(self, text, reply_markup=None, parse_mode=None):
        self.sent.append((text, reply_markup))


def upd(msg, user_id=ALLOWED):
    return SimpleNamespace(effective_user=SimpleNamespace(id=user_id), message=msg, callback_query=None)


def test_commands_are_registered_in_menu_and_help():
    svc, *_ = make()
    names = {n for n, _ in COMMANDS}
    assert {"projet", "projets", "annuler"} <= names
    text = svc.help()[0].text
    assert "/projet " in text and "/projets " in text and "/annuler " in text


def test_projet_starts_the_interview_and_projets_shows_the_overview():
    svc, coach, _ = make()
    r = svc.projet()[0]
    assert "Nouveau projet" in r.text and coach.calls[-1] == ("interview_start",)
    r = svc.projets()[0]
    assert "Tes projets" in r.text and r.buttons and coach.calls[-1] == ("projects_overview",)


def test_annuler_closes_the_interview():
    svc, coach, _ = make()
    assert "annulé" in svc.annuler()[0].text and coach.calls[-1] == ("interview_cancel",)


def test_text_goes_to_the_interview_when_open_and_is_not_saved_as_note():
    svc, coach, mem = make()
    journal = []
    svc.journal = lambda text, source: journal.append(text)
    coach.interview_open = True
    replies = svc.on_text("Projet IA : entraîner un modèle")
    assert [r.text for r in replies] == ["réponse de l'entretien"] and replies[0].buttons
    assert mem.notes == [] and coach.extract_calls == [] and journal == []
    assert coach.calls[-1] == ("interview_text", "Projet IA : entraîner un modèle")


def test_voice_goes_to_the_interview_too():
    svc, coach, mem = make()
    coach.interview_open = True
    replies = svc.on_voice("/tmp/a.ogg")
    assert mem.notes == [] and any("réponse de l'entretien" in r.text for r in replies)


def test_text_is_unchanged_without_interview():
    svc, coach, mem = make([FakeTask("t1", "Envoyer le devis")])
    replies = svc.on_text("Penser à envoyer le devis")
    assert len(mem.notes) == 1 and "Note enregistrée" in replies[0].text


def test_interview_not_ready_falls_back_to_normal_text():
    svc, coach, mem = make()

    def boom():
        raise NotReadyError("coach.store indisponible")

    coach.interview_active = boom
    svc.on_text("juste une idée")
    assert len(mem.notes) == 1


def test_callbacks_are_routed():
    svc, coach, _ = make()
    r = svc.on_callback("pi:cat:IA")
    assert coach.calls[-1] == ("interview_callback", "cat:IA") and r.text == "réponse de l'entretien"
    svc.on_callback("pjv:abcd1234")
    assert coach.calls[-1] == ("project_action", "pjv", "abcd1234")
    svc.on_callback("pm:ok:abcd1234")
    assert coach.calls[-1] == ("project_action", "pm", "ok:abcd1234")
    svc.on_callback("pp:end:abcd1234")
    assert coach.calls[-1] == ("project_action", "pp", "end:abcd1234")
    svc.on_callback("pjok:t1")
    assert coach.calls[-1] == ("project_pick", "pjok", "t1", None)
    svc.on_callback("pjset:t1:abcd1234")
    assert coach.calls[-1] == ("project_pick", "pjset", "t1", "abcd1234")
    svc.on_callback("pjms:t1:-")
    assert coach.calls[-1] == ("project_pick", "pjms", "t1", "-")
    svc.on_callback("pjno:t1")
    assert coach.calls[-1] == ("project_pick", "pjno", "t1", None)


def test_interview_callback_with_several_messages_shows_them_all():
    svc, coach, _ = make()
    coach.interview_replies = [("⚠️ Catégorie prise", []), ("Quelle catégorie ?", [[("Aucune", "pi:nocat")]])]
    r = svc.on_callback("pi:ok")
    assert "Catégorie prise" in r.text and "Quelle catégorie ?" in r.text and r.buttons == [[("Aucune", "pi:nocat")]]


def test_task_card_carries_the_project_proposal():
    svc, coach, _ = make([FakeTask("t1", "Nettoyer le dataset")])
    coach.proposals["t1"] = ("📁 <b>Projet IA</b> · étape <i>Données</i> ?", [[("Oui", "pjok:t1")]])
    replies = svc.on_text("Penser à nettoyer le dataset")
    card = replies[-1]
    assert "Projet IA" in card.text and ("Oui", "pjok:t1") in [b for row in card.buttons for b in row]
    assert ("Planifier", "plan:t1") in [b for row in card.buttons for b in row]    # les boutons habituels restent


def test_handlers_projet_projets_annuler_and_foreign_user():
    svc, coach, _ = make()
    h = TelegramHandlers(svc)
    ctx = SimpleNamespace(args=[])
    for fn, expected in ((h.projet, "Nouveau projet"), (h.projets, "Tes projets"), (h.annuler, "annulé")):
        m = Msg()
        asyncio.run(fn(upd(m), ctx))
        assert len(m.sent) == 1 and expected in m.sent[0][0]
    calls = len(coach.calls)
    for fn in (h.projet, h.projets, h.annuler):
        m = Msg()
        asyncio.run(fn(upd(m, user_id=999), ctx))
        assert m.sent == []
    assert len(coach.calls) == calls


def test_interview_active_crash_falls_back_to_normal_text():
    svc, coach, mem = make()

    def boom():
        raise RuntimeError("database is locked")

    coach.interview_active = boom
    replies = svc.on_text("juste une idée")
    assert len(mem.notes) == 1 and "Note enregistrée" in replies[0].text


def test_empty_interview_reply_does_not_drop_the_message():
    svc, coach, mem = make()
    coach.interview_open = True
    coach.interview_replies = []  # le brouillon a expiré entre-temps
    replies = svc.on_text("juste une idée")
    assert len(mem.notes) == 1 and "Note enregistrée" in replies[0].text


def test_interview_text_not_ready_falls_back_to_normal_text():
    svc, coach, mem = make()
    coach.interview_open = True

    def boom(text):
        raise NotReadyError("coach.store indisponible")

    coach.interview_text = boom
    svc.on_text("juste une idée")
    assert len(mem.notes) == 1


def test_interview_text_unexpected_error_falls_back_to_normal_text():
    svc, coach, mem = make()
    coach.interview_open = True

    def boom(text):
        raise RuntimeError("database is locked")

    coach.interview_text = boom
    replies = svc.on_text("juste une idée")
    assert len(mem.notes) == 1 and "Note enregistrée" in replies[0].text


def test_messages_sent_in_a_row_during_an_interview_are_handled_as_one():
    import asyncio
    from types import SimpleNamespace
    from bot.handlers import TelegramHandlers
    from tests.bot.test_bot_handlers import Msg, upd, ctx

    svc, coach, _ = make()
    coach.interview_open = True
    h = TelegramHandlers(svc, text_wait=0.05)
    msgs = [Msg("début"), Msg("milieu"), Msg("fin")]

    async def go():
        await asyncio.gather(*(h.text(upd(m), ctx()) for m in msgs))

    asyncio.run(go())
    texts = [c[1] for c in coach.calls if c[0] == "interview_text"]
    assert texts == ["début\nmilieu\nfin"]
    assert [len(m.sent) for m in msgs] == [0, 0, 1]   # une seule réponse, sous le dernier message


def test_no_wait_when_no_interview_is_open():
    import asyncio
    from bot.handlers import TelegramHandlers
    from tests.bot.test_bot_handlers import Msg, upd, ctx

    svc, coach, _ = make()
    h = TelegramHandlers(svc, text_wait=30)   # si on attendait, le test durerait 30 s
    asyncio.run(h.text(upd(Msg("bonjour")), ctx()))
