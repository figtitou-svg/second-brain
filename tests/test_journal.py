"""Journal Google Docs, commande /note, coche immédiate dans Notion."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import journal
import notion_sync
from bot.fakes import FakeCoach, FakeMemory, FakeRouter
from bot.handlers import INFO_RE, BotService
from coach import store
from coach.calendar_client import FakeCalendarClient
from coach.db import Database
from coach.models import Block, Task
from coach.scheduler import CoachScheduler
from tests.coach.conftest import FROZEN
from coach.rules import load_rules

TZ = ZoneInfo("Europe/Paris")
T0 = datetime(2026, 10, 16, 14, 32, tzinfo=TZ)


# --- rendu ----------------------------------------------------------------------------------

def test_render_adds_a_heading_once_per_day():
    entries = [journal.Entry(T0, "texte", "Le code du garage est 4721"),
               journal.Entry(T0 + timedelta(minutes=3), "vocal", "Appeler la banque"),
               journal.Entry(T0 + timedelta(days=1), "note", "Idée de projet")]
    text, headers, last = journal.render(entries, None)
    lines = text.splitlines()
    assert lines[0] == "vendredi 16 octobre 2026"
    assert lines[1] == "14:32 · texte — Le code du garage est 4721"
    assert lines[2] == "14:35 · vocal — Appeler la banque"
    assert lines[3] == "samedi 17 octobre 2026" and len(headers) == 2 and last == "2026-10-17"
    for s, e in headers:  # les positions encadrent bien les titres (retour à la ligne compris)
        assert text[s:e].strip() in ("vendredi 16 octobre 2026", "samedi 17 octobre 2026")


def test_render_continues_same_day_without_new_heading():
    text, headers, _ = journal.render([journal.Entry(T0, "texte", "suite")], "2026-10-16")
    assert headers == [] and text == "14:32 · texte — suite\n"


# --- envoi vers Google Docs ------------------------------------------------------------------

class FakeDocs:
    def __init__(self, fail=False):
        self.fail, self.requests, self.size = fail, [], 1

    def documents(self):
        return self

    def get(self, documentId, fields):
        class R:
            def __init__(s, size): s.size = size
            def execute(s): return {"body": {"content": [{"endIndex": s.size}]}}
        return R(self.size)

    def batchUpdate(self, documentId, body):
        outer = self

        class R:
            def execute(s):
                if outer.fail:
                    raise RuntimeError("réseau coupé")
                outer.requests.append(body["requests"])
                outer.size += len(body["requests"][0]["insertText"]["text"])
        return R()


class FakeDrive:
    class svc:  # noqa: N801
        @staticmethod
        def files():
            class F:
                def list(self, **k): return type("R", (), {"execute": lambda s: {"files": [{"id": "DOC1"}]}})()
            return F()

    def folder(self, path): return "ROOT"


def isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(journal, "PENDING", tmp_path / "pending.jsonl")
    monkeypatch.setattr(journal, "STATE", tmp_path / "state.json")


def test_entries_queued_then_flushed_in_one_write(monkeypatch, tmp_path):
    isolate(monkeypatch, tmp_path)
    journal.add("première info", "texte", T0, flush=False)
    journal.add("deuxième info", "vocal", T0 + timedelta(minutes=1), flush=False)
    docs = FakeDocs()
    assert journal.flush_pending((FakeDrive(), docs)) == 2
    (reqs,) = docs.requests
    assert "première info" in reqs[0]["insertText"]["text"] and "deuxième info" in reqs[0]["insertText"]["text"]
    assert any(r["updateParagraphStyle"]["paragraphStyle"]["namedStyleType"] == "HEADING_2" for r in reqs[1:])
    assert journal.pending() == [] and journal.flush_pending((FakeDrive(), docs)) == 0


def test_failed_send_keeps_entries_and_retry_succeeds(monkeypatch, tmp_path):
    isolate(monkeypatch, tmp_path)
    journal.add("à ne pas perdre", "texte", T0, flush=False)
    try:
        journal.flush_pending((FakeDrive(), FakeDocs(fail=True)))
        raise AssertionError("aurait dû échouer")
    except RuntimeError:
        pass
    assert [e.text for e in journal.pending()] == ["à ne pas perdre"]  # toujours en file
    assert journal.flush_pending((FakeDrive(), FakeDocs())) == 1


def test_day_heading_not_repeated_across_flushes(monkeypatch, tmp_path):
    isolate(monkeypatch, tmp_path)
    docs = FakeDocs()
    for i in range(2):
        journal.add(f"info {i}", "texte", T0 + timedelta(minutes=i), flush=False)
        journal.flush_pending((FakeDrive(), docs))
    headings = [r for reqs in docs.requests for r in reqs[1:]
                if r["updateParagraphStyle"]["paragraphStyle"]["namedStyleType"] == "HEADING_2"]
    assert len(headings) == 1


# --- /note et préfixes ---------------------------------------------------------------------------

def test_note_keeps_info_only_no_task_no_event():
    coach, mem, logged = FakeCoach(), FakeMemory(), []
    svc = BotService(coach, mem, FakeRouter(), 1, journal=lambda t, s: logged.append((t, s)))
    (r,) = svc.note("le code du garage est 4721")
    assert logged == [("le code du garage est 4721", "note")] and mem.notes
    assert coach.extract_calls == [] and "Noté" in r.text and "journal" in r.text


def test_prefix_triggers_info_only_even_for_voice():
    coach, mem, logged = FakeCoach(), FakeMemory(), []
    svc = BotService(coach, mem, FakeRouter(), 1, journal=lambda t, s: logged.append((t, s)))
    for phrase in ("note : rdv médecin vendredi 15h", "Info - mon numéro de dossier est 123", "retiens: x"):
        assert INFO_RE.match(phrase)
    svc.on_text("note : rdv médecin vendredi 15h", source="vocal")
    assert coach.extract_calls == [] and logged == [("rdv médecin vendredi 15h", "note")]


def test_normal_text_and_voice_are_journaled_with_their_source():
    coach, mem, logged = FakeCoach(), FakeMemory(), []
    svc = BotService(coach, mem, FakeRouter("appeler la banque lundi"), 1, journal=lambda t, s: logged.append((t, s)))
    svc.on_text("Envoyer le devis demain")
    svc.on_voice(__file__)
    assert logged == [("Envoyer le devis demain", "texte"), ("appeler la banque lundi", "vocal")]


def test_journal_failure_never_blocks_the_note():
    def broken(t, s):
        raise RuntimeError("docs down")
    coach, mem = FakeCoach(), FakeMemory()
    replies = BotService(coach, mem, FakeRouter(), 1, journal=broken).on_text("une info")
    assert mem.notes and "Note enregistrée" in replies[0].text


def test_questions_are_not_journaled():
    logged = []
    BotService(FakeCoach(), FakeMemory(), FakeRouter(), 1,
               journal=lambda t, s: logged.append(t)).on_text("j'ai quoi cet aprem ?")
    assert logged == []


# --- Notion : coche immédiate -----------------------------------------------------------------------

class FakeNotion:
    def __init__(self): self.done = []
    def fetch_all(self): return []
    def set_done(self, page_id): self.done.append(page_id)


def sched_env():
    db = Database(":memory:")
    s = CoachScheduler(db, FakeCalendarClient(), load_rules(FROZEN), lambda t, b: None, now_fn=lambda: T0)
    store.configure(db, s)
    return db, s


def test_done_via_command_checks_notion_immediately():
    db, s = sched_env()
    notion = FakeNotion()
    s.on_task_done = lambda tid: notion_sync.push_done(db, notion, tid)
    db.upsert_task(Task(id="nt-1", title="[C] TP3", source_id="notion:page-abc", estimated_minutes=60))
    db.upsert_task(Task(id="local", title="Autre", estimated_minutes=60))
    assert store.mark_done("nt-1") and notion.done == ["page-abc"]
    assert store.mark_done("local") and notion.done == ["page-abc"]  # tâche non Notion : rien envoyé


def test_done_via_button_checks_notion_immediately():
    db, s = sched_env()
    notion = FakeNotion()
    s.on_task_done = lambda tid: notion_sync.push_done(db, notion, tid)
    db.upsert_task(Task(id="nt-2", title="[EPS] Mail", source_id="notion:page-xyz", estimated_minutes=60))
    blk = Block(id="b1", task_id="nt-2", start=T0, end=T0 + timedelta(hours=1))
    db.add_block(blk)
    s.mark_done("b1")  # bouton « Oui » de « C'est fait ? »
    assert db.get_task("nt-2").status == "done" and notion.done == ["page-xyz"]


def test_notion_failure_does_not_break_marking_done():
    db, s = sched_env()

    class Down(FakeNotion):
        def set_done(self, page_id): raise RuntimeError("Notion down")
    s.on_task_done = lambda tid: notion_sync.push_done(db, Down(), tid)
    db.upsert_task(Task(id="nt-3", title="[Algo] X", source_id="notion:page-q", estimated_minutes=60))
    assert store.mark_done("nt-3") and db.get_task("nt-3").status == "done"  # le sync de 15 min rattrapera
