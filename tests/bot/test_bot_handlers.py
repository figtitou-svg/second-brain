from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from bot.fakes import FakeCoach, FakeMemory, FakeRouter, FakeTask
from bot.handlers import BotService, TelegramHandlers
from bot.ports import NotReadyError

ALLOWED = 42


def make(tasks=None):
    coach = FakeCoach(tasks or [])
    mem = FakeMemory()
    router = FakeRouter("acheter du pain demain")
    return BotService(coach, mem, router, ALLOWED), coach, mem, router


def run(coro):
    return asyncio.run(coro)


def test_free_text_is_private_note_and_extracts_tasks(monkeypatch):
    monkeypatch.setenv("ALLOW_CLOUD_FOR_NOTES", "false")
    svc, coach, mem, _ = make([FakeTask("t1", "Envoyer le devis")])
    replies = svc.on_text("Penser à envoyer le devis")
    assert mem.notes[0][1] == "private"
    assert coach.extract_calls[0][2] == "private"
    labels = [b[0] for row in replies[-1].buttons for b in row]
    assert labels == ["Planifier", "Modifier la durée", "Ignorer"]
    assert "Envoyer le devis" in replies[-1].text


def test_free_text_without_tasks_just_confirms():
    svc, _, mem, _ = make()
    replies = svc.on_text("juste une idée")
    assert len(replies) == 1 and not replies[0].buttons
    assert len(mem.notes) == 1


def test_voice_is_transcribed_then_handled_as_text():
    svc, coach, mem, router = make()
    svc.on_voice("/tmp/a.ogg")
    assert router.transcribed == ["/tmp/a.ogg"]
    assert mem.notes[0][0] == "acheter du pain demain"
    assert coach.extract_calls


def test_pdf_ambiguous_asks_privacy_then_applies():
    svc, _, mem, _ = make()
    replies = svc.on_document("/tmp/x.pdf", "x.pdf", None, "pdf")
    assert mem.docs["doc1"]["sensitivity"] == "private"  # défaut sûr en attendant
    cb = {b[1] for row in replies[-1].buttons for b in row}
    assert cb == {"pub:doc1", "prv:doc1"}
    svc.on_callback("pub:doc1")
    assert mem.docs["doc1"]["sensitivity"] == "public"


def test_document_obvious_public_no_question():
    svc, _, mem, _ = make()
    replies = svc.on_document("/tmp/c.pdf", "cours_thermo.pdf", "cours", "pdf")
    assert mem.docs["doc1"]["sensitivity"] == "public"
    assert not replies[-1].buttons


def test_document_obvious_private():
    svc, _, mem, _ = make()
    svc.on_document("/tmp/c.pdf", "x.pdf", "mes notes perso", "pdf")
    assert mem.docs["doc1"]["sensitivity"] == "private"


def test_ask_returns_answer_with_source():
    svc, _, mem, _ = make()
    r = svc.ask("c'est quoi ?")
    assert "réponse" in r[0].text and "note:1" in r[0].text
    assert mem.questions == ["c'est quoi ?"]


def test_ask_without_question_gives_usage():
    svc, _, mem, _ = make()
    assert "Usage" in svc.ask("  ")[0].text and not mem.questions


def test_today_and_tasks():
    svc, coach, _, _ = make([FakeTask("t1", "A")])
    coach.extract_tasks("x", "s", "private")
    assert "Programme" in svc.today()[0].text
    header, card = svc.tasks()
    assert "(1)" in header.text and "A" in card.text
    assert [cb for _, cb in card.buttons[0]] == ["td:t1", "tpo:t1", "tnw:t1", "ign:t1"]


def test_tasks_empty():
    svc, *_ = make()
    assert "Aucune" in svc.tasks()[0].text


def test_done():
    svc, coach, _, _ = make([FakeTask("t1", "A")])
    coach.extract_tasks("x", "s", "private")
    assert "fait" in svc.done("t1")[0].text.lower()
    assert coach.tasks["t1"].status == "done"
    assert "introuvable" in svc.done("zz")[0].text.lower()
    assert "Usage" in svc.done("")[0].text


def test_task_callbacks():
    svc, coach, _, _ = make([FakeTask("t1", "A")])
    coach.extract_tasks("x", "s", "private")
    assert "Planifié" in svc.on_callback("plan:t1").text
    r = svc.on_callback("dur:t1")
    assert any(b[1] == "durset:t1:90" for row in r.buttons for b in row)
    assert [b[0] for row in r.buttons for b in row] == ["10 min", "15 min", "30 min", "45 min", "1 h", "1 h 30", "2 h", "3 h"]
    assert len(r.buttons) == 2
    r = svc.on_callback("durset:t1:90")
    assert coach.tasks["t1"].estimated_minutes == 90
    assert [b[0] for row in r.buttons for b in row][0] == "Planifier"
    svc.on_callback("ign:t1")
    assert coach.tasks["t1"].status == "dropped"


def test_reminder_callbacks():
    svc, coach, _, _ = make()
    svc.on_callback("ok:b1")
    svc.on_callback("nf:b1")
    svc.on_callback("rep:b1")
    svc.on_callback("nw:b1")
    assert [c[2] for c in coach.calls] == ["done", "not_finished", "rescheduled", "next_week"]


def test_task_card_buttons():
    svc, coach, _, _ = make([FakeTask("t1", "A")])
    coach.extract_tasks("x", "s", "private")
    svc.on_callback("tpo:t1")
    svc.on_callback("tnw:t1")
    assert [c[1:] for c in coach.calls] == [("t1", "tomorrow"), ("t1", "next_week")]
    assert "A" in svc.on_callback("td:t1").text and coach.tasks["t1"].status == "done"


def test_unknown_callback():
    svc, *_ = make()
    assert svc.on_callback("???").text


def test_not_ready_module_is_reported():
    svc, coach, _, _ = make()

    def boom(*a, **k):
        raise NotReadyError("coach")

    coach.extract_tasks = boom
    r = svc.on_text("hello")
    assert "indisponible" in r[-1].text.lower()


def test_reminder_keyboard_helper():
    from bot.handlers import reminder_buttons

    assert [b[0] for row in reminder_buttons("b9") for b in row] == ["Oui", "Pas fini", "Reporter", "Semaine pro."]


# --- couche Telegram (objets factices, aucun réseau) ---

class Msg:
    def __init__(self, text=None, **kw):
        self.text = text
        self.caption = kw.get("caption")
        self.sent: list[tuple] = []
        self.voice = kw.get("voice")
        self.photo = kw.get("photo")
        self.document = kw.get("document")

    async def reply_text(self, text, reply_markup=None, parse_mode=None):
        self.sent.append((text, reply_markup))


class FakeFile:
    def __init__(self):
        self.path = None

    async def download_to_drive(self, path):
        self.path = str(path)
        with open(path, "wb") as f:
            f.write(b"x")


class Media:
    def __init__(self, mime_type=None, file_name=None):
        self.mime_type = mime_type
        self.file_name = file_name
        self.file = FakeFile()

    async def get_file(self):
        return self.file


def upd(msg, user_id=ALLOWED, callback=None):
    return SimpleNamespace(effective_user=SimpleNamespace(id=user_id), message=msg, callback_query=callback)


def ctx(args=None):
    return SimpleNamespace(args=args or [])


def test_foreign_user_ignored_everywhere():
    svc, _, mem, _ = make()
    h = TelegramHandlers(svc)
    m = Msg("salut")
    for fn in (h.text, h.voice, h.document, h.ask, h.today, h.tasks, h.done):
        run(fn(upd(m, user_id=999), ctx(["x"])))
    q = SimpleNamespace(data="ign:t1", answered=False, edited=[])

    async def answer(text=None):
        q.answered = True

    q.answer = answer
    run(h.callback(upd(None, 999, q), ctx()))
    assert m.sent == [] and mem.notes == [] and not q.answered


def test_no_user_ignored():
    svc, *_ = make()
    h = TelegramHandlers(svc)
    run(h.text(SimpleNamespace(effective_user=None, message=Msg("x"), callback_query=None), ctx()))


def test_text_handler_replies_with_markup_buttons():
    svc, *_ = make([FakeTask("t1", "A")])
    h = TelegramHandlers(svc)
    m = Msg("tâche")
    run(h.text(upd(m), ctx()))
    assert len(m.sent) == 2 and m.sent[1][1] is not None


def test_ask_handler_uses_args():
    svc, _, mem, _ = make()
    h = TelegramHandlers(svc)
    m = Msg("/ask a b")
    run(h.ask(upd(m), ctx(["a", "b"])))
    assert mem.questions == ["a b"]


def test_voice_handler_downloads_and_transcribes():
    svc, _, mem, router = make()
    h = TelegramHandlers(svc)
    m = Msg(voice=Media())
    run(h.voice(upd(m), ctx()))
    assert len(router.transcribed) == 1
    assert mem.notes


def test_document_handler_pdf():
    svc, _, mem, _ = make()
    h = TelegramHandlers(svc)
    m = Msg(document=Media("application/pdf", "c.pdf"), caption=None)
    run(h.document(upd(m), ctx()))
    assert mem.docs["doc1"]["kind"] == "pdf"


def test_photo_handler():
    svc, _, mem, _ = make()
    h = TelegramHandlers(svc)
    m = Msg(photo=[Media(), Media()], caption="recette")
    run(h.document(upd(m), ctx()))
    assert mem.docs["doc1"]["kind"] == "photo"
    assert mem.docs["doc1"]["sensitivity"] == "public"


def test_callback_handler_edits_message():
    svc, coach, _, _ = make([FakeTask("t1", "A")])
    coach.extract_tasks("x", "s", "private")
    h = TelegramHandlers(svc)
    state = {}

    async def answer(text=None):
        state["answered"] = True

    async def edit(text, reply_markup=None, parse_mode=None):
        state["text"] = text

    q = SimpleNamespace(data="plan:t1", answer=answer, edit_message_text=edit)
    run(h.callback(upd(None, ALLOWED, q), ctx()))
    assert state["answered"] and "Planifié" in state["text"]


def test_callback_shows_pending_then_confirmation():
    svc, coach, _, _ = make([FakeTask("t1", "A")])
    coach.extract_tasks("x", "s", "private")
    h = TelegramHandlers(svc)
    state = {"steps": []}

    async def answer(text=None):
        state["steps"].append("answer")

    async def pending(reply_markup=None):
        state["steps"].append("pending")

    async def edit(text, reply_markup=None, parse_mode=None):
        state["steps"].append("edit")
        state["text"] = text

    q = SimpleNamespace(data="plan:t1", answer=answer, edit_message_reply_markup=pending,
                        edit_message_text=edit)
    run(h.callback(upd(None, ALLOWED, q), ctx()))
    assert state["steps"] == ["answer", "pending", "edit"]
    assert state["text"].startswith("✅")


def test_callback_failure_is_reported():
    svc, _, _, _ = make([])
    h = TelegramHandlers(svc)

    def boom(data):
        raise RuntimeError("boom")

    svc.on_callback = boom
    state = {}

    async def answer(text=None):
        pass

    async def edit(text, reply_markup=None, parse_mode=None):
        state["text"] = text

    q = SimpleNamespace(data="plan:t1", answer=answer, edit_message_text=edit)
    run(h.callback(upd(None, ALLOWED, q), ctx()))
    assert state["text"].startswith("⚠️")


def test_help_liste_toutes_les_commandes():
    from bot.handlers import COMMANDS
    svc = BotService(FakeCoach(), FakeMemory(), FakeRouter(), 1)
    text = svc.help()[0].text
    for name, _ in COMMANDS:
        assert f"/{name}" in text
    assert {"ask", "today", "tasks", "replan", "focus", "projet", "projets", "annuler", "semaine", "note", "done",
            "quota", "help"} == {n for n, _ in COMMANDS}


def test_document_temp_file_is_deleted():
    import os
    svc, *_ = make()
    h = TelegramHandlers(svc)
    media = Media("application/pdf", "c.pdf")
    run(h.document(upd(Msg(document=media, caption=None)), ctx()))
    assert media.file.path and not os.path.exists(media.file.path)
