# tests/web/test_actions.py
from __future__ import annotations

from bot.fakes import FakeCoach, FakeMemory, FakeRouter, FakeTask
from bot.handlers import BotService, Reply
from web import actions


def svc():
    coach = FakeCoach([FakeTask("t1", "Envoyer le devis")])
    return BotService(coach, FakeMemory(), FakeRouter("acheter du pain demain"), 42), coach


def test_safe_html_keeps_telegram_tags_and_escapes_others():
    out = actions.safe_html('<b>Gras</b> <i>it</i> <code>x</code>\n<a href="https://ex.org">lien</a>')
    assert out == '<b>Gras</b> <i>it</i> <code>x</code><br><a href="https://ex.org">lien</a>'
    assert actions.safe_html("<script>alert(1)</script>") == "&lt;script&gt;alert(1)&lt;/script&gt;"
    assert "&lt;a href=&quot;javascript" in actions.safe_html('<a href="javascript:alert(1)">x</a>')
    assert actions.safe_html('<b onclick="x()">t</b>') == '&lt;b onclick=&quot;x()&quot;&gt;t</b>'


def test_commands_map_to_the_same_methods_as_telegram(monkeypatch):
    s, _ = svc()
    called = []
    for name in ("today", "tasks", "replan", "focus", "projet", "projets", "annuler", "week", "quota", "help"):
        monkeypatch.setattr(s, name, lambda n=name: called.append(n) or [Reply(n)])
    for cmd in ("/today", "/tasks", "/replan", "/focus", "/projet", "/projets", "/annuler", "/semaine", "/quota",
                "/help", "/start"):
        actions.submit(s, cmd)
    assert called == ["today", "tasks", "replan", "focus", "projet", "projets", "annuler", "week", "quota", "help",
                      "help"]


def test_commands_with_arguments(monkeypatch):
    s, _ = svc()
    seen = []
    monkeypatch.setattr(s, "ask", lambda q: seen.append(("ask", q)) or [Reply("r")])
    monkeypatch.setattr(s, "note", lambda t: seen.append(("note", t)) or [Reply("r")])
    monkeypatch.setattr(s, "done", lambda i: seen.append(("done", i)) or [Reply("r")])
    actions.submit(s, "/ask  j'ai quoi demain ?")
    actions.submit(s, "/note le code est 4721")
    actions.submit(s, "/done t1 en trop")
    actions.submit(s, "/done")
    assert seen == [("ask", "j'ai quoi demain ?"), ("note", "le code est 4721"), ("done", "t1"), ("done", "")]


def test_unknown_command_and_free_text(monkeypatch):
    s, _ = svc()
    assert "Commande inconnue" in actions.submit(s, "/zorglub")[0].text
    seen = []
    monkeypatch.setattr(s, "on_text", lambda text, source="texte": seen.append((text, source)) or [Reply("ok")])
    actions.submit(s, "Penser à envoyer le devis")
    assert seen == [("Penser à envoyer le devis", "web")]


def test_empty_text_does_nothing():
    s, _ = svc()
    assert actions.submit(s, "   ") == []


def test_click_uses_on_callback_and_reports_errors(monkeypatch):
    s, _ = svc()
    monkeypatch.setattr(s, "on_callback", lambda data: Reply(f"ok {data}"))
    assert actions.click(s, "td:t1").text == "ok td:t1"

    def boom(data):
        raise RuntimeError("panne")

    monkeypatch.setattr(s, "on_callback", boom)
    assert actions.click(s, "td:t1").text == actions.CLICK_FAILED


def test_discussion_keeps_last_50():
    d = actions.Discussion(limit=50)
    for i in range(55):
        d.add(f"m{i}", [Reply(f"r{i}")])
    items = d.items()
    assert len(items) == 50 and items[0].said == "m5" and items[-1].replies[0].text == "r54"
