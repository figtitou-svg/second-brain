# tests/web/test_server.py
from __future__ import annotations

import asyncio
import logging
import re
import socket
import time
import urllib.request
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from bot.fakes import FakeCoach, FakeMemory, FakeRouter, FakeTask
from bot.handlers import BotService, Reply
from coach.models import Block, Project, Task
from coach.calendar_client import FakeCalendarClient
from coach.db import Database
from coach.rules import load_rules
from coach.scheduler import CoachScheduler
from tests.coach.conftest import FROZEN, dt
from web import data, server, system

PASSWORD = "mot-de-passe-de-test"
NOW = dt(2026, 10, 27, 10, 27)


def make_app(clock=None, svc=None, home_fn=None, machine_fn=None):
    db = Database(":memory:")
    cal = FakeCalendarClient()
    sched = CoachScheduler(db, cal, load_rules(FROZEN), lambda t, b=None: None, lambda: NOW)
    svc = svc or BotService(FakeCoach([FakeTask("t1", "Envoyer le devis")]), FakeMemory(), FakeRouter("x"), 42)
    deps = server.Deps(
        svc=svc,
        home=home_fn or (lambda: data.build_home(db, sched, data.AgendaCache(cal.agenda), NOW)),
        machine=machine_fn or (lambda: system.machine()),
        errors=system.ErrorLog(),
        components=lambda: [],
        quota=lambda: "📊 <b>Quotas</b>")
    return server.create_app(deps, PASSWORD, "0" * 64, clock=clock or (lambda: 1_800_000_000.0)), svc


def run(app, scenario):
    async def go():
        async with TestClient(TestServer(app)) as client:
            return await scenario(client)
    return asyncio.run(go())


async def login(client, password=PASSWORD):
    return await client.post("/login", data={"password": password}, allow_redirects=False)


async def csrf_of(client):
    page = await (await client.get("/")).text()
    return page.split('name="csrf" content="')[1].split('"')[0]


def test_pages_require_login():
    app, _ = make_app()

    async def scenario(c):
        r = await c.get("/", allow_redirects=False)
        assert r.status == 302 and r.headers["Location"] == "/login"
        assert (await c.get("/login")).status == 200
    run(app, scenario)


def test_login_sets_strict_cookie_and_opens_home():
    app, _ = make_app()

    async def scenario(c):
        r = await login(c)
        assert r.status == 302 and r.headers["Location"] == "/"
        cookie = r.headers["Set-Cookie"]
        assert "sb_session=" in cookie and "HttpOnly" in cookie and "SameSite=Strict" in cookie
        assert "Max-Age=2592000" in cookie
        home = await c.get("/")
        assert home.status == 200 and "Second Brain" in await home.text()
    run(app, scenario)


def test_wrong_password_then_lockout():
    t = [1_800_000_000.0]
    app, _ = make_app(clock=lambda: t[0])

    async def scenario(c):
        for _ in range(5):
            r = await login(c, "faux")
            assert r.status == 200 and "Mot de passe incorrect" in await r.text()
        r = await login(c)
        assert r.status == 429                       # même le bon mot de passe est bloqué
        t[0] += 15 * 60 + 1
        assert (await login(c)).status == 302        # débloqué après 15 min
    run(app, scenario)


def test_lockout_holds_against_slow_concurrent_bodies():
    app, _ = make_app()

    async def go():
        async with TestServer(app) as srv:
            async def open_slow(password):
                body = f"password={password}".encode()
                reader, writer = await asyncio.open_connection(srv.host, srv.port)
                writer.write(b"POST /login HTTP/1.1\r\nHost: %s:%d\r\n"
                             b"Content-Type: application/x-www-form-urlencoded\r\n"
                             b"Content-Length: %d\r\nConnection: close\r\n\r\n"
                             % (srv.host.encode(), srv.port, len(body)) + body[:3])
                await writer.drain()
                return reader, writer, body[3:]

            async def finish(conn):
                reader, writer, rest = conn
                writer.write(rest)
                await writer.drain()
                raw = await reader.read()
                writer.close()
                return int(raw.split(b" ", 2)[1])

            slow = [await open_slow("faux") for _ in range(8)]
            await asyncio.sleep(0.05)                # le serveur a reçu tous les en-têtes
            statuses = await asyncio.gather(*(finish(c) for c in slow))
            right = await finish(await open_slow(PASSWORD))
            return statuses, right

    statuses, right = asyncio.run(go())
    assert statuses.count(200) <= 5 and statuses.count(429) == 8 - statuses.count(200)
    assert right == 429


def test_tampered_or_expired_cookie_is_refused():
    t = [1_800_000_000.0]
    app, _ = make_app(clock=lambda: t[0])

    async def scenario(c):
        r = await login(c)
        value = r.cookies["sb_session"].value
        c.session.cookie_jar.clear()                 # on envoie le cookie à la main
        tampered = value[:-1] + ("0" if value[-1] != "0" else "1")

        async def status(cookie):
            return (await c.get("/", allow_redirects=False, headers={"Cookie": f"sb_session={cookie}"})).status

        assert await status(tampered) == 302
        assert await status(value) == 200
        t[0] += 30 * 24 * 3600 + 1
        assert await status(value) == 302             # expiré
    run(app, scenario)


def test_click_without_session_is_refused():
    calls = []
    s = BotService(FakeCoach([]), FakeMemory(), FakeRouter("x"), 42)
    s.on_callback = lambda d: calls.append(d) or Reply("ok")
    app, _ = make_app(svc=s)

    async def scenario(c):
        r = await c.post("/click", data={"data": "td:t1"}, headers={"X-CSRF": "x"})
        assert r.status == 401 and calls == []
    run(app, scenario)


def test_click_requires_csrf_and_same_origin():
    calls = []
    s = BotService(FakeCoach([]), FakeMemory(), FakeRouter("x"), 42)
    s.on_callback = lambda d: calls.append(d) or Reply("<b>Fait</b>")
    app, _ = make_app(svc=s)

    async def scenario(c):
        await login(c)
        token = await csrf_of(c)
        assert (await c.post("/click", data={"data": "td:t1"})).status == 403
        r = await c.post("/click", data={"data": "td:t1"}, headers={"X-CSRF": token, "Origin": "http://evil.example"})
        assert r.status == 403 and calls == []
        r = await c.post("/click", data={"data": "td:t1"}, headers={"X-CSRF": token})
        assert r.status == 200 and "<b>Fait</b>" in await r.text() and calls == ["td:t1"]
    run(app, scenario)


def test_say_adds_to_discussion():
    s = BotService(FakeCoach([]), FakeMemory(), FakeRouter("x"), 42)
    s.focus = lambda: [Reply("🎯 Tes priorités")]
    app, _ = make_app(svc=s)

    async def scenario(c):
        await login(c)
        token = await csrf_of(c)
        r = await c.post("/say", data={"text": "/focus"}, headers={"X-CSRF": token})
        assert r.status == 200 and "Tes priorités" in await r.text()
        page = await (await c.get("/discussion")).text()
        assert "/focus" in page and "Tes priorités" in page
    run(app, scenario)


def test_failing_section_renders_error_fragment():
    def boom():
        raise RuntimeError("base verrouillée")
    app, _ = make_app(home_fn=boom)

    async def scenario(c):
        await login(c)
        r = await c.get("/fragment/home")
        assert r.status == 200 and "indisponible" in await r.text()
        assert (await c.get("/systeme")).status == 200
    run(app, scenario)


def test_secret_is_created_once(tmp_path):
    p = tmp_path / "web_secret"
    a = server.load_secret(p)
    assert len(a) == 64 and server.load_secret(p) == a


def test_start_without_password_does_nothing(caplog):
    assert server.start(None, env={}) is None
    assert "WEB_PASSWORD" in caplog.text


def test_start_serves_login(tmp_path):
    app, _ = make_app()
    thread = server.start(None, env={"WEB_PASSWORD": PASSWORD, "WEB_HOST": "127.0.0.1", "WEB_PORT": "0"},
                          app=app)
    assert thread is not None and thread.daemon
    with urllib.request.urlopen(f"http://127.0.0.1:{thread.port}/login", timeout=5) as r:
        assert r.status == 200


def test_no_external_resources_in_templates_and_css():
    root = Path(server.HERE)
    for f in list((root / "templates").glob("*.html")) + [root / "static" / "app.css", root / "static" / "app.js"]:
        text = f.read_text(encoding="utf-8")
        assert not re.search(r"(src|href)=\"https?://", text), f
        assert "@import" not in text and "url(http" not in text and "//fonts." not in text, f


def test_menu_lists_all_sections_with_future_ones_disabled():
    app, _ = make_app()

    async def scenario(c):
        await login(c)
        page = await (await c.get("/")).text()
        for name in ("Accueil", "Agenda", "Focus", "Tâches", "Projets", "Récap semaine", "À trier", "Suggestions",
                     "Notifications", "Discussion", "Notes et documents", "Système"):
            assert name in page, name
        assert page.count("bientôt") == 9          # les 9 sections sans lien du menu latéral
        assert 'href="/discussion"' in page and 'href="/systeme"' in page
    run(app, scenario)


def test_home_shows_now_ring_with_block_buttons():
    db = Database(":memory:")
    cal = FakeCalendarClient()
    sched = CoachScheduler(db, cal, load_rules(FROZEN), lambda t, b=None: None, lambda: NOW)
    p = Project(name="Projet IA")
    db.save_project(p)
    t = Task(title="[IA] Entraîner le modèle", project_id=p.id, status="scheduled")
    db.upsert_task(t)
    b = Block(task_id=t.id, start=dt(2026, 10, 27, 9, 30), end=dt(2026, 10, 27, 11, 19))
    db.add_block(b)
    app, _ = make_app(home_fn=lambda: data.build_home(db, sched, data.AgendaCache(cal.agenda), NOW))

    async def scenario(c):
        await login(c)
        page = await (await c.get("/")).text()
        assert "Entraîner le modèle" in page and ">52<" in page   # <text … data-countdown>52</text>
        assert f'data-cb="ok:{b.id}"' in page and f'data-cb="rep:{b.id}"' in page
        assert f'data-cb="pjv:{p.id}"' in page
        assert data.project_colors(db)[p.id] in page
        frag = await (await c.get("/fragment/home")).text()
        assert ">52<" in frag and "<nav" not in frag            # le fragment ne contient que le corps
    run(app, scenario)


def test_home_empty_state_offers_replan():
    app, _ = make_app()

    async def scenario(c):
        await login(c)
        page = await (await c.get("/")).text()
        assert "Rien de prévu pour l'instant" in page and 'data-say="/replan"' in page
    run(app, scenario)


def test_system_page_shows_machine_components_and_errors():
    import health
    s = BotService(FakeCoach([]), FakeMemory(), FakeRouter("x"), 42)
    errors = system.ErrorLog()
    errors.handle(logging.makeLogRecord(
        {"name": "telegram", "levelno": 40, "levelname": "ERROR", "msg": "Conflict: terminated"}))
    deps = server.Deps(s, lambda: None, lambda: system.machine(), errors,
                       lambda: [health.ComponentState("gmail", "Gmail", False, None, 3, "délai dépassé")],
                       lambda: "📊 <b>Groq</b> 312/1000")
    app = server.create_app(deps, PASSWORD, "0" * 64, clock=lambda: 1_800_000_000.0)

    async def scenario(c):
        await login(c)
        page = await (await c.get("/systeme")).text()
        assert "Gmail" in page and "délai dépassé" in page and "Conflict: terminated" in page
        assert "<b>Groq</b>" in page and "Disque" in page
    run(app, scenario)


def test_discussion_lists_command_shortcuts_and_reply_buttons():
    s = BotService(FakeCoach([]), FakeMemory(), FakeRouter("x"), 42)
    s.focus = lambda: [Reply("🎯 Tes priorités", [[("Fait", "td:t1")]])]
    app, _ = make_app(svc=s)

    async def scenario(c):
        await login(c)
        token = await csrf_of(c)
        await c.post("/say", data={"text": "/focus"}, headers={"X-CSRF": token})
        page = await (await c.get("/discussion")).text()
        assert 'data-say="/replan"' in page and 'data-cb="td:t1"' in page
    run(app, scenario)


def test_zero_progress_ring_draws_no_arc():
    rings = [data.Ring("p0", "Recherche", "#3CC7A6", 0, None, False, None),
             data.Ring("p3", "Projet IA", "#9B8CFF", 33, 10, False, None)]
    home = data.Home(NOW, NOW, NOW, 50.0, None, [], [], rings, data.Counters(0, 0, 0))
    app, _ = make_app(home_fn=lambda: home)

    async def scenario(c):
        await login(c)
        page = await (await c.get("/")).text()
        zero = page.split('data-cb="pjv:p0"')[1].split("</button>")[0]
        third = page.split('data-cb="pjv:p3"')[1].split("</button>")[0]
        assert 'class="arc"' not in zero and 'class="arc"' in third and 'stroke-dashoffset="67"' in third
    run(app, scenario)


def test_system_component_states_are_honest():
    import health
    s = BotService(FakeCoach([]), FakeMemory(), FakeRouter("x"), 42)
    states = [health.ComponentState("drive", "Drive", True, None, 0, None),
              health.ComponentState("cal", "Agenda", True, NOW, 0, None),
              health.ComponentState("gmail", "Gmail", False, None, 1, "délai dépassé"),
              health.ComponentState("groq", "Groq", False, None, 3, None)]
    deps = server.Deps(s, lambda: None, lambda: system.machine(), system.ErrorLog(), lambda: states, lambda: "")
    app = server.create_app(deps, PASSWORD, "0" * 64, clock=lambda: 1_800_000_000.0)

    async def scenario(c):
        await login(c)
        page = await (await c.get("/systeme")).text()
        items = {li.split("</span> ")[1].split("</span>")[0]: li
                 for li in page.split('<ul class="components">')[1].split("</ul>")[0].split("<li ")[1:]}
        assert "⚪" in items["Drive"] and "✅" not in items["Drive"] and "pas encore lancé" in items["Drive"]
        assert "✅" in items["Agenda"] and "OK à" in items["Agenda"]
        assert "❌" in items["Gmail"] and "(1 échec)" in items["Gmail"] and "délai dépassé" in items["Gmail"]
        assert "❌" in items["Groq"] and "(3 échecs)" in items["Groq"] and "None" not in items["Groq"]
    run(app, scenario)


def test_failing_system_page_keeps_the_layout():
    def boom():
        raise RuntimeError("/proc illisible")
    app, _ = make_app(machine_fn=boom)

    async def scenario(c):
        await login(c)
        r = await c.get("/systeme")
        page = await r.text()
        assert r.status == 200 and 'id="nav"' in page and "<!doctype html>" in page
        assert "indisponible" in page and 'role="alert"' in page
    run(app, scenario)


def test_security_headers_on_every_response():
    app, _ = make_app()

    async def scenario(c):
        responses = [await c.get("/login")]
        responses.append(await login(c))                        # redirection 302
        responses += [await c.get("/"), await c.get("/fragment/home"), await c.get("/static/app.css"),
                      await c.get("/nexiste-pas")]
        for r in responses:
            csp = r.headers.get("Content-Security-Policy", "")
            assert "default-src 'self'" in csp and "script-src 'self'" in csp, r.url
            assert "frame-ancestors 'none'" in csp and "base-uri 'none'" in csp and "form-action 'self'" in csp
            assert r.headers["X-Content-Type-Options"] == "nosniff"
            assert r.headers["Referrer-Policy"] == "no-referrer"
    run(app, scenario)


def _occupied_port():
    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):                   # Windows : personne d'autre ne peut s'y lier
        holder.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    holder.bind(("127.0.0.1", 0))
    holder.listen()
    return holder, holder.getsockname()[1]


def test_bind_retry_serves_once_the_port_is_freed(caplog):
    caplog.set_level(logging.WARNING, "web.server")
    holder, port = _occupied_port()
    app, _ = make_app()
    thread = server.WebThread(app, "127.0.0.1", port, retry_seconds=0.2, retry_max=20)
    thread.start()
    try:
        assert thread.ready.wait(5) and not thread.started_ok and thread.retrying and thread.is_alive()
        holder.close()
        assert thread.serving.wait(10) and thread.started_ok
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/login", timeout=5) as r:
            assert r.status == 200
        assert "nouvelle tentative" in caplog.text
    finally:
        holder.close()
        if thread.loop and not thread.loop.is_closed():
            thread.loop.call_soon_threadsafe(thread.loop.stop)
        thread.join(5)
    assert not thread.is_alive() and thread.loop.is_closed()


def test_bind_retry_gives_up_after_max_tries(caplog):
    caplog.set_level(logging.WARNING, "web.server")
    holder, port = _occupied_port()
    try:
        app, _ = make_app()
        thread = server.WebThread(app, "127.0.0.1", port, retry_seconds=0.01, retry_max=3)
        thread.start()
        thread.join(10)
    finally:
        holder.close()
    assert not thread.is_alive() and not thread.started_ok and thread.loop.is_closed()
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and "nouvelle tentative" in r.getMessage()]
    errors = [r for r in caplog.records if r.levelno == logging.ERROR and "abandon" in r.getMessage()]
    assert len(warnings) == 3 and len(errors) == 1


def test_start_returns_the_retrying_thread_promptly(monkeypatch, caplog):
    monkeypatch.setattr(server, "BIND_RETRY_SECONDS", 0.5)   # assez long pour que le fil réessaie encore
    monkeypatch.setattr(server, "BIND_RETRY_MAX", 2)
    holder, port = _occupied_port()
    try:
        app, _ = make_app()
        t0 = time.monotonic()
        thread = server.start(None, env={"WEB_PASSWORD": PASSWORD, "WEB_PORT": str(port)}, app=app)
        assert thread is not None and time.monotonic() - t0 < 5
        assert "nouvelle tentative" in caplog.text
        thread.join(10)
    finally:
        holder.close()
    assert not thread.is_alive() and thread.loop.is_closed()
