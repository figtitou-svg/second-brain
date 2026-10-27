# web/server.py
"""Serveur de la page web : connexion, protection, routes ; tourne dans un fil à côté du bot Telegram."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

import jinja2
from aiohttp import web
from markupsafe import Markup   # dépendance de Jinja2

from bot.handlers import COMMANDS, BotService
from web import actions

log = logging.getLogger("web.server")

HERE = Path(__file__).parent
SESSION_DAYS = 30
MAX_FAILS, LOCK_SECONDS = 5, 15 * 60
COOKIE = "sb_session"
SECRET_PATH = Path("data") / "web_secret"
UNAVAILABLE = "Cette section est indisponible pour le moment."
_HEX64 = re.compile(r"[0-9a-f]{64}")
_COOKIE_VALUE = re.compile(r"([0-9]{1,12})\.([0-9a-f]{32})\.([0-9a-f]{64})")


@dataclass
class Deps:
    svc: BotService
    home: Callable[[], Any]           # -> data.Home
    machine: Callable[[], Any]        # -> system.Machine
    errors: Any                       # system.ErrorLog
    components: Callable[[], list]    # -> list[health.ComponentState]
    quota: Callable[[], str]          # -> HTML Telegram de /quota


def load_secret(path: Path = SECRET_PATH) -> str:
    """Secret de signature des cookies : lu dans `path`, ou créé (64 hex, lisible par le seul propriétaire)."""
    try:
        value = path.read_text(encoding="utf-8").strip()
        if _HEX64.fullmatch(value):
            return value
    except OSError:
        pass
    value = secrets.token_hex(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(value)
    return value


def _sign(secret: str, payload: str) -> str:
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


def _same(a: str, b: str) -> bool:
    """Comparaison à temps constant ; accepte n'importe quel texte (compare_digest refuse le non-ASCII en str)."""
    return hmac.compare_digest(a.encode("utf-8", "surrogateescape"), b.encode("utf-8", "surrogateescape"))


def create_app(deps: Deps, password: str, secret: str, clock: Callable[[], float] = time.time) -> web.Application:
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(HERE / "templates"), autoescape=True)
    env.filters["tg"] = lambda text: Markup(actions.safe_html(text))   # HTML Telegram filtré par safe_html
    discussion = actions.Discussion()
    fails: list[float] = []

    def session(request: web.Request) -> str | None:
        """Jeton CSRF de la session si le cookie est valide, sinon None."""
        m = _COOKIE_VALUE.fullmatch(request.cookies.get(COOKIE, ""))
        if m is None:
            return None
        expiry, csrf, sig = m.groups()
        if not _same(sig, _sign(secret, f"{expiry}.{csrf}")) or int(expiry) <= clock():
            return None
        return csrf

    def render(name: str, request: web.Request, **ctx: Any) -> str:
        return env.get_template(name).render(csrf=session(request) or "", path=request.path, **ctx)

    def page(name: str, request: web.Request, status: int = 200, **ctx: Any) -> web.Response:
        return web.Response(text=render(name, request, **ctx), status=status, content_type="text/html",
                            charset="utf-8")

    def error_fragment(request: web.Request, message: str = UNAVAILABLE) -> web.Response:
        return page("_error.html", request, message=message)

    async def blocking(fn: Callable, *args: Any) -> Any:
        return await asyncio.get_running_loop().run_in_executor(None, fn, *args)

    def protected(handler: Callable) -> Callable:
        async def wrapped(request: web.Request) -> web.StreamResponse:
            csrf = session(request)
            if csrf is None:
                if request.method == "GET":
                    raise web.HTTPFound("/login")
                raise web.HTTPUnauthorized(text="Session expirée : reconnecte-toi.")
            if request.method == "POST":
                origin = request.headers.get("Origin")
                if origin is not None and origin.lower() != f"{request.scheme}://{request.host}".lower():
                    raise web.HTTPForbidden(text="Origine refusée.")
                if not _same(request.headers.get("X-CSRF", ""), csrf):
                    raise web.HTTPForbidden(text="Jeton de sécurité manquant : recharge la page.")
            return await handler(request)
        return wrapped

    def locked() -> float:
        """Secondes de blocage restantes (0 si la connexion est permise)."""
        now = clock()
        fails[:] = [t for t in fails if now - t < LOCK_SECONDS]
        # bloqué tant qu'il reste MAX_FAILS échecs récents : on attend l'expiration du plus ancien d'entre eux
        return (fails[-MAX_FAILS] + LOCK_SECONDS - now) if len(fails) >= MAX_FAILS else 0

    async def login_form(request: web.Request) -> web.Response:
        return page("login.html", request)

    async def login(request: web.Request) -> web.Response:
        form = await request.post()
        # aucun `await` entre la vérification du blocage et l'enregistrement de l'échec : une requête dont le
        # corps arrive lentement ne peut pas passer le contrôle avant que les échecs concurrents soient comptés
        wait = locked()
        if wait:
            return page("login.html", request, status=429,
                        error=f"Trop d'essais. Réessaie dans {int(wait // 60) + 1} min.")
        if not _same(str(form.get("password", "")), password):
            fails.append(clock())
            log.warning("page web : mot de passe incorrect")
            return page("login.html", request, error="Mot de passe incorrect.")
        fails.clear()
        expiry, csrf = str(int(clock()) + SESSION_DAYS * 86400), secrets.token_hex(16)
        resp = web.HTTPFound("/")
        resp.set_cookie(COOKIE, f"{expiry}.{csrf}.{_sign(secret, f'{expiry}.{csrf}')}",
                        max_age=SESSION_DAYS * 86400, httponly=True, samesite="Strict", path="/")
        raise resp

    async def logout(request: web.Request) -> web.Response:
        resp = web.HTTPFound("/login")
        resp.del_cookie(COOKIE, path="/")
        raise resp

    async def section(request: web.Request, template: str, fn: Callable, key: str) -> web.Response:
        try:
            value = await blocking(fn)
            return page(template, request, **{key: value})
        except Exception:  # noqa: BLE001 - une section en panne n'empêche pas les autres
            log.exception("section web %s illisible", key)
            return error_fragment(request)

    async def home(request: web.Request) -> web.Response:
        try:
            value = await blocking(deps.home)
        except Exception:  # noqa: BLE001
            log.exception("accueil web illisible")
            value = None
        try:
            return page("home.html", request, home=value)
        except Exception:  # noqa: BLE001 - données inattendues : la page reste servie, sans l'accueil
            log.exception("accueil web : rendu impossible")
            return page("home.html", request, home=None)

    async def home_fragment(request: web.Request) -> web.Response:
        return await section(request, "_home_body.html", deps.home, "home")

    async def discussion_page(request: web.Request) -> web.Response:
        return page("discussion.html", request, exchanges=discussion.items(), commands=COMMANDS)

    async def system_page(request: web.Request) -> web.Response:
        def gather() -> dict:
            return {"machine": deps.machine(), "errors": deps.errors.entries()[:20],
                    "components": deps.components(), "quota": deps.quota()}
        return await section(request, "system.html", gather, "sys")

    async def say(request: web.Request) -> web.Response:
        text = str((await request.post()).get("text", ""))
        try:
            replies = await blocking(actions.submit, deps.svc, text)
        except Exception:  # noqa: BLE001 - jamais de trace Python à l'écran
            log.exception("saisie web échouée")
            return error_fragment(request, actions.CLICK_FAILED)
        ex = discussion.add(text.strip(), replies) if text.strip() else None
        return page("_exchange.html", request, ex=ex)

    async def click(request: web.Request) -> web.Response:
        data = str((await request.post()).get("data", ""))
        reply = await blocking(actions.click, deps.svc, data)
        return page("_exchange.html", request, ex=actions.Exchange(time.time(), "", [reply]))

    app = web.Application()
    app.add_routes([
        web.get("/login", login_form), web.post("/login", login), web.post("/logout", protected(logout)),
        web.get("/", protected(home)), web.get("/fragment/home", protected(home_fragment)),
        web.get("/discussion", protected(discussion_page)), web.get("/systeme", protected(system_page)),
        web.post("/say", protected(say)), web.post("/click", protected(click)),
        web.static("/static", HERE / "static"),
    ])
    return app


class WebThread(threading.Thread):
    def __init__(self, app: web.Application, host: str, port: int) -> None:
        super().__init__(name="web", daemon=True)
        self.app, self.host, self.port = app, host, port
        self.ready = threading.Event()
        self.started_ok = False

    def run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            runner = web.AppRunner(self.app, access_log=None)
            loop.run_until_complete(runner.setup())
            site = web.TCPSite(runner, self.host, self.port)
            loop.run_until_complete(site.start())
            self.port = runner.addresses[0][1]   # port réel (0 en test)
            log.info("page web : http://%s:%d", self.host, self.port)
        except Exception:  # noqa: BLE001 - le bot continue sans la page web
            log.exception("page web : démarrage impossible")
            self.ready.set()
            loop.close()
            return
        self.started_ok = True
        self.ready.set()
        loop.run_forever()


def start(deps: Deps | None, env: Mapping[str, str] | None = None, app: web.Application | None = None) -> WebThread | None:
    """Démarre la page web dans un fil démon ; None (bot intact) si WEB_PASSWORD manque ou si le port est pris."""
    env = os.environ if env is None else env
    password = env.get("WEB_PASSWORD", "").strip()
    if not password:
        log.warning("page web désactivée : WEB_PASSWORD absent du .env")
        return None
    try:
        host = env.get("WEB_HOST", "127.0.0.1").strip() or "127.0.0.1"
        port = int(env.get("WEB_PORT", "8080").strip() or 8080)
        if app is None:
            app = create_app(deps, password, load_secret())
    except Exception:  # noqa: BLE001 - réglage invalide ou secret illisible : le bot continue sans la page web
        log.exception("page web désactivée : configuration invalide")
        return None
    thread = WebThread(app, host, port)
    thread.start()
    if not thread.ready.wait(10) or not thread.started_ok:
        return None
    return thread
