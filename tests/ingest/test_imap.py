from datetime import date
from email.message import EmailMessage

import pytest

import ingest_imap
import memory

CFG = {"host": "imap.example", "user": "moi", "password": "secret", "port": 993, "folder": "INBOX", "days": 2}


def raw_mail(subject="TD de 3IF", body="Rendre le TP avant vendredi", html=False):
    m = EmailMessage()
    m["From"] = "Prof <prof@insa-lyon.fr>"
    m["Subject"] = subject
    if html:
        m.set_content("<p>Rendre&nbsp;le <b>TP</b></p><script>x()</script>", subtype="html")
    else:
        m.set_content(body)
    return m.as_bytes()


class FakeImap:
    def __init__(self, mails, login_ok=True):
        self.mails, self.login_ok, self.calls = mails, login_ok, []
        self.logged_out = False

    def login(self, user, pw):
        self.calls.append(("login", user))
        return ("OK", [b"ok"]) if self.login_ok else ("NO", [b"bad"])

    def select(self, folder, readonly=False):
        self.calls.append(("select", folder, readonly))
        return "OK", [str(len(self.mails)).encode()]

    def response(self, code):
        return code, [b"777"]

    def uid(self, cmd, *args):
        self.calls.append((cmd, *args))
        if cmd == "SEARCH":
            return "OK", [b" ".join(self.mails)]
        uid = args[0]
        return "OK", [(b"1 (UID " + uid + b" BODY[] {9}", self.mails[uid]), b")"]

    def logout(self):
        self.logged_out = True


@pytest.fixture
def mem(monkeypatch, tmp_path):
    monkeypatch.setattr(memory, "DB_PATH", tmp_path / "m.sqlite")
    monkeypatch.setattr(memory, "_conn", None)
    monkeypatch.setenv("ALLOW_CLOUD_FOR_MAILS", "false")
    seen = []
    monkeypatch.setattr(ingest_imap.process_mail.__globals__["store"], "extract_items",
                        lambda t, s, sens: (seen.append((t, s, sens)) or [], []))
    return seen


def test_new_mails_processed_once_read_only(mem):
    imap = FakeImap({b"5": raw_mail(), b"6": raw_mail("Réunion")})
    assert ingest_imap.poll(lambda *a: None, CFG, lambda h, p: imap, date(2026, 11, 11)) == 2
    assert ("select", "INBOX", True) in imap.calls                       # boîte ouverte en lecture seule
    assert ("SEARCH", None, "SINCE 09-Nov-2026") in imap.calls
    assert all(c[2] == "(BODY.PEEK[])" for c in imap.calls if c[0] == "FETCH")   # jamais marqué comme lu
    assert [s for _, s, _ in mem] == ["insa:777:5", "insa:777:6"] and mem[0][2] == "private"
    assert "De : Prof <prof@insa-lyon.fr>" in mem[0][0] and "Rendre le TP" in mem[0][0]
    assert imap.logged_out
    imap2 = FakeImap({b"5": raw_mail(), b"6": raw_mail("Réunion")})
    assert ingest_imap.poll(lambda *a: None, CFG, lambda h, p: imap2, date(2026, 11, 11)) == 0
    assert not [c for c in imap2.calls if c[0] == "FETCH"]               # déjà vus : pas relus


def test_html_mail_is_turned_into_text(mem):
    imap = FakeImap({b"1": raw_mail(html=True)})
    ingest_imap.poll(lambda *a: None, CFG, lambda h, p: imap, date(2026, 11, 11))
    text = mem[0][0]
    assert "Rendre le TP" in text and "<b>" not in text and "x()" not in text


def test_failed_login_raises_and_logs_out(mem):
    imap = FakeImap({b"1": raw_mail()}, login_ok=False)
    with pytest.raises(Exception):
        ingest_imap.poll(lambda *a: None, CFG, lambda h, p: imap, date(2026, 11, 11))
    assert imap.logged_out and not mem


def test_extraction_failure_retries_the_mail_next_time(monkeypatch, tmp_path):
    monkeypatch.setattr(memory, "DB_PATH", tmp_path / "m.sqlite")
    monkeypatch.setattr(memory, "_conn", None)
    store = ingest_imap.process_mail.__globals__["store"]
    monkeypatch.setattr(store, "extract_items", lambda t, s, sens: (_ for _ in ()).throw(RuntimeError("modèle muet")))
    with pytest.raises(RuntimeError):
        ingest_imap.poll(lambda *a: None, CFG, lambda h, p: FakeImap({b"1": raw_mail()}), date(2026, 11, 11))
    monkeypatch.setattr(store, "extract_items", lambda t, s, sens: ([], []))
    assert ingest_imap.poll(lambda *a: None, CFG, lambda h, p: FakeImap({b"1": raw_mail()}), date(2026, 11, 11)) == 1


def test_disabled_without_credentials(monkeypatch):
    for k in ("INSA_IMAP_HOST", "INSA_IMAP_USER", "INSA_IMAP_PASSWORD"):
        monkeypatch.delenv(k, raising=False)
    assert ingest_imap.config() is None
    assert ingest_imap.poll(lambda *a: None) == 0
    monkeypatch.setenv("INSA_IMAP_HOST", "h")
    monkeypatch.setenv("INSA_IMAP_USER", "u")
    monkeypatch.setenv("INSA_IMAP_PASSWORD", "p")
    assert ingest_imap.config()["port"] == 993 and ingest_imap.config()["folder"] == "INBOX"
