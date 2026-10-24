import base64

import ingest_gmail
import memory


class _Exec:
    def __init__(self, v): self.v = v
    def execute(self): return self.v


class FakeGmail:
    def users(self): return self
    def messages(self): return self
    def list(self, **k): return _Exec({"messages": [{"id": "m1"}]})
    def get(self, **k):
        data = base64.urlsafe_b64encode("Envoyer le devis".encode()).decode()
        return _Exec({"payload": {"mimeType": "text/plain", "body": {"data": data},
                                  "headers": [{"name": "Subject", "value": "Devis"},
                                              {"name": "From", "value": "bde@insa.fr"}]}})


def _run(monkeypatch, tmp_path, cloud):
    monkeypatch.setattr(memory, "DB_PATH", tmp_path / "m.sqlite")
    monkeypatch.setattr(memory, "_conn", None)
    monkeypatch.setenv("ALLOW_CLOUD_FOR_MAILS", cloud)
    seen = []
    monkeypatch.setattr(ingest_gmail.store, "extract_items", lambda t, s, sens: (seen.append(sens) or [], []))
    assert ingest_gmail.poll(FakeGmail(), lambda *a: None) == 1
    assert ingest_gmail.poll(FakeGmail(), lambda *a: None) == 0  # déjà vu
    return seen, memory._db().execute("SELECT sensitivity FROM docs").fetchone()[0]


def test_mails_private_by_default(monkeypatch, tmp_path):
    assert _run(monkeypatch, tmp_path, "false") == (["private"], "private")


def test_mails_public_when_opted_in(monkeypatch, tmp_path):
    assert _run(monkeypatch, tmp_path, "true") == (["public"], "public")


def test_mail_retried_when_extraction_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(memory, "DB_PATH", tmp_path / "m.sqlite")
    monkeypatch.setattr(memory, "_conn", None)
    calls = []

    def flaky(t, s, sens):
        calls.append(s)
        if len(calls) == 1:
            raise RuntimeError("quota épuisé")
        return [], []

    monkeypatch.setattr(ingest_gmail.store, "extract_items", flaky)
    try:
        ingest_gmail.poll(FakeGmail(), lambda *a: None)
    except RuntimeError:
        pass
    assert not memory.has_source("gmail:m1")  # pas marqué comme vu : il sera retraité
    assert ingest_gmail.poll(FakeGmail(), lambda *a: None) == 1
    assert calls == ["gmail:m1", "gmail:m1"] and memory.has_source("gmail:m1")


def _poll_with_task(monkeypatch, tmp_path, proposal):
    from bot.fakes import FakeTask
    monkeypatch.setattr(memory, "DB_PATH", tmp_path / "m.sqlite")
    monkeypatch.setattr(memory, "_conn", None)
    monkeypatch.setattr(ingest_gmail.store, "extract_items",
                        lambda t, s, sens: ([FakeTask("t1", "Envoyer le devis")], []))
    monkeypatch.setattr(ingest_gmail.store, "project_proposal", proposal)
    sent = []
    n = ingest_gmail.poll(FakeGmail(), lambda text, buttons=None: sent.append((text, buttons)))
    return n, sent


def test_mail_card_survives_a_failing_project_proposal(monkeypatch, tmp_path):
    def boom(task_id):
        raise RuntimeError("coach.store non configuré")

    n, sent = _poll_with_task(monkeypatch, tmp_path, boom)
    assert n == 1 and len(sent) == 1
    assert "Envoyer le devis" in sent[0][0] and sent[0][1] == ingest_gmail.task_buttons("t1")


def test_mail_card_carries_the_project_proposal(monkeypatch, tmp_path):
    n, sent = _poll_with_task(monkeypatch, tmp_path, lambda tid: ("📁 <b>Projet IA</b> ?", [[("Oui", "pjok:x")]]))
    text, buttons = sent[0]
    flat = [b for row in buttons for b in row]
    assert "📁 <b>Projet IA</b> ?" in text
    assert ("Oui", "pjok:x") in flat and ("Planifier", "plan:t1") in flat
