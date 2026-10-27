# tests/web/test_server.py
from __future__ import annotations

import asyncio
import urllib.request

from aiohttp.test_utils import TestClient, TestServer

from bot.fakes import FakeCoach, FakeMemory, FakeRouter, FakeTask
from bot.handlers import BotService, Reply
from coach.calendar_client import FakeCalendarClient
from coach.db import Database
from coach.rules import load_rules
from coach.scheduler import CoachScheduler
from tests.coach.conftest import FROZEN, dt
from web import data, server, system

PASSWORD = "mot-de-passe-de-test"
NOW = dt(2026, 10, 27, 10, 27)


def make_app(clock=None, svc=None, home_fn=None):
    db = Database(":memory:")
    cal = FakeCalendarClient()
    sched = CoachScheduler(db, cal, load_rules(FROZEN), lambda t, b=None: None, lambda: NOW)
    svc = svc or BotService(FakeCoach([FakeTask("t1", "Envoyer le devis")]), FakeMemory(), FakeRouter("x"), 42)
    deps = server.Deps(
        svc=svc,
        home=home_fn or (lambda: data.build_home(db, sched, data.AgendaCache(cal.agenda), NOW)),
        machine=lambda: system.machine(),
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
