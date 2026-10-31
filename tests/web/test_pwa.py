# tests/web/test_pwa.py
from __future__ import annotations

import json
import struct
from pathlib import Path

from tests.web.test_pages import make
from tests.web.test_server import login, run

STATIC = Path(__file__).resolve().parent.parent.parent / "web" / "static"


def png_size(path: Path) -> tuple[int, int, int]:
    raw = path.read_bytes()
    assert raw[:8] == b"\x89PNG\r\n\x1a\n"
    width, height = struct.unpack(">II", raw[16:24])
    return width, height, raw[25]                    # 25 : type de couleur (6 = RGBA, 2 = RGB)


def test_manifest_is_valid_and_points_to_existing_icons():
    m = json.loads((STATIC / "manifest.webmanifest").read_text(encoding="utf-8"))
    assert m["name"] == "Second Brain" and m["start_url"] == "/" and m["scope"] == "/" and m["display"] == "standalone"
    sizes = {(i["sizes"], i["purpose"]) for i in m["icons"]}
    assert sizes == {("192x192", "any"), ("512x512", "any"), ("512x512", "maskable")}
    for icon in m["icons"]:
        path = STATIC / icon["src"].removeprefix("/static/")
        w, h, _ = png_size(path)
        assert f"{w}x{h}" == icon["sizes"]


def test_apple_touch_icon_is_opaque_180():
    w, h, color = png_size(STATIC / "icons" / "apple-touch-icon.png")
    assert (w, h, color) == (180, 180, 2)


def test_head_tags_on_login_and_pages_and_public_files():
    app, _ = make()

    async def scenario(c):
        login_page = await (await c.get("/login")).text()
        await login(c)
        home = await (await c.get("/")).text()
        for html in (login_page, home):
            for needle in ('rel="manifest"', 'rel="icon"', 'rel="apple-touch-icon"', 'name="theme-color"',
                           'apple-mobile-web-app-capable', 'name="mobile-web-app-capable"',
                           'apple-mobile-web-app-title', 'apple-mobile-web-app-status-bar-style'):
                assert needle in html, needle
            assert html.count('name="theme-color"') == 2
    run(app, scenario)

    async def public(c):                              # sans connexion
        r = await c.get("/static/manifest.webmanifest")
        assert r.status == 200 and "json" in r.headers["Content-Type"]
        for name in ("icon-192.png", "icon-512.png", "icon-maskable-512.png", "apple-touch-icon.png"):
            assert (await c.get(f"/static/icons/{name}")).status == 200
        assert (await c.get("/static/icons/icon.svg")).status == 200
    app, _ = make()                                   # une appli ne se rejoue pas sur une autre boucle
    run(app, public)
