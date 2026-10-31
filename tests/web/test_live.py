# tests/web/test_live.py
from __future__ import annotations

import asyncio
import threading

from aiohttp.test_utils import TestClient, TestServer

from bot.handlers import Reply
from tests.web.test_pages import make
from tests.web.test_server import csrf_of, login
from web import live, server


def test_publish_without_loop_or_subscriber_only_calls_listeners_and_never_raises():
    bus, seen = live.Bus(), []
    bus.add_listener(lambda kind, data: seen.append((kind, data)))
    bus.add_listener(lambda kind, data: 1 / 0)               # un écouteur en panne ne gêne personne
    bus.publish("bot", {"id": 1})
    assert seen == [("bot", {"id": 1})]


def test_streams_are_capped_at_four_and_slow_ones_are_cut():
    async def go():
        bus = live.Bus()
        bus.attach(asyncio.get_running_loop())
        streams = [bus.open() for _ in range(5)]
        assert streams[0].queue.get_nowait() is None            # le plus ancien est fermé
        for _ in range(live.QUEUE_SIZE + 5):                     # un abonné qui ne lit jamais
            bus.publish("changed")
        await asyncio.sleep(0.05)
        slow = streams[1]
        items = []
        while not slow.queue.empty():
            items.append(slow.queue.get_nowait())
        assert items[-1] is None and len(items) <= live.QUEUE_SIZE + 1
    asyncio.run(go())


def test_publish_from_another_thread_reaches_the_stream():
    async def go():
        bus = live.Bus()
        bus.attach(asyncio.get_running_loop())
        stream = bus.open()
        threading.Thread(target=lambda: bus.publish("bot", {"id": 7, "text": "x"})).start()
        assert await asyncio.wait_for(stream.queue.get(), 2) == ("bot", {"id": 7, "text": "x"})
    asyncio.run(go())


def test_events_requires_login_and_streams_only_ids(monkeypatch):
    monkeypatch.setattr(server, "HEARTBEAT", 0.2)
    bus = live.Bus()
    app, _ = make(bus=bus)

    async def go():
        async with TestClient(TestServer(app)) as c:
            assert (await c.get("/events", allow_redirects=False)).status == 302
            await login(c)
            r = await c.get("/events")
            assert r.status == 200 and r.headers["Content-Type"].startswith("text/event-stream")
            assert r.headers["Cache-Control"] == "no-store"
            assert (await r.content.readline()) == b"retry: 5000\n"
            await r.content.readline()
            bus.publish("bot", {"id": 3, "text": "<script>secret</script>", "buttons": []})
            seen = b""
            while b"event: bot" not in seen:
                seen += await asyncio.wait_for(r.content.readline(), 3)
            data = await asyncio.wait_for(r.content.readline(), 3)
            assert data == b'data: {"id": 3}\n'                  # jamais de texte dans le flux
            bus.publish("changed")
            seen = b""
            while b"event: changed" not in seen:
                seen += await asyncio.wait_for(r.content.readline(), 3)
            assert b"data: {}" in await asyncio.wait_for(r.content.readline(), 3)
            line = b""
            while b": ping" not in line:                          # battement
                line = await asyncio.wait_for(r.content.readline(), 3)
            r.close()
    asyncio.run(go())


def test_events_without_bus_is_404():
    app, _ = make()

    async def go():
        async with TestClient(TestServer(app)) as c:
            await login(c)
            assert (await c.get("/events")).status == 404
    asyncio.run(go())


def test_bot_message_lands_in_the_discussion_and_the_fragment_is_escaped():
    bus = live.Bus()
    app, _ = make(bus=bus)

    async def go():
        async with TestClient(TestServer(app)) as c:
            await login(c)
            bus.publish("bot", {"id": 5, "text": "Rappel <b>bosse</b> <script>x</script>",
                                "buttons": [[("Fait", "ok:b1")]]})
            frag = await c.get("/fragment/message/5")
            html = await frag.text()
            assert frag.status == 200 and 'data-ref="5"' in html
            assert "<b>bosse</b>" in html and "<script>" not in html and "&lt;script&gt;" in html
            assert 'data-cb="ok:b1"' in html
            page = await (await c.get("/discussion")).text()
            assert 'data-ref="5"' in page                          # reste affiché au rechargement
            assert (await c.get("/fragment/message/999")).status == 404
            assert (await c.get("/fragment/message/abc")).status == 404
            assert (await c.get("/fragment/message/5", allow_redirects=False)).status == 200
    asyncio.run(go())


def test_successful_click_and_say_publish_changed():
    bus = live.Bus()
    kinds: list[str] = []
    bus.add_listener(lambda kind, data: kinds.append(kind))
    app, _ = make(bus=bus)

    async def go():
        async with TestClient(TestServer(app)) as c:
            await login(c)
            token = await csrf_of(c)
            await c.post("/say", data={"text": "/help"}, headers={"X-CSRF": token})
            await c.post("/click", data={"data": "pjv:inconnu"}, headers={"X-CSRF": token})
    asyncio.run(go())
    assert kinds.count("changed") == 2


def test_failed_say_does_not_publish_changed(monkeypatch):
    def boom(svc, text):
        raise RuntimeError("service en panne")
    monkeypatch.setattr(server.actions, "submit", boom)
    bus = live.Bus()
    kinds: list[str] = []
    bus.add_listener(lambda kind, data: kinds.append(kind))
    app, _ = make(bus=bus)

    async def go():
        async with TestClient(TestServer(app)) as c:
            await login(c)
            token = await csrf_of(c)
            r = await c.post("/say", data={"text": "x"}, headers={"X-CSRF": token})
            assert r.status == 200                                # fragment d'erreur, pas d'exception
    asyncio.run(go())
    assert "changed" not in kinds
