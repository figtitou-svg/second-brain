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
from datetime import MAXYEAR, MINYEAR, date, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any, Callable, Mapping

import jinja2
from aiohttp import web
from markupsafe import Markup   # dépendance de Jinja2

from bot.handlers import COMMANDS, BotService, Reply
from coach import fmt
from web import actions

log = logging.getLogger("web.server")

HERE = Path(__file__).parent
SESSION_DAYS = 30
MAX_FAILS, LOCK_SECONDS = 5, 15 * 60
COOKIE = "sb_session"
SECRET_PATH = Path("data") / "web_secret"
UNAVAILABLE = "Cette section est indisponible pour le moment."
BIND_RETRY_SECONDS, BIND_RETRY_MAX = 30, 20   # adresse pas encore prête (Tailscale au démarrage) : 10 min d'essais
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                               "img-src 'self' data:; base-uri 'none'; form-action 'self'; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}
_HEX64 = re.compile(r"[0-9a-f]{64}")
_COOKIE_VALUE = re.compile(r"([0-9]{1,12})\.([0-9a-f]{32})\.([0-9a-f]{64})")
_ISO_DAY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
AGENDA_VUES = ("jour", "semaine")
PROJECT_NOT_FOUND = "Projet introuvable."


@dataclass
class Deps:
    svc: BotService
    home: Callable[[], Any]           # -> data.Home
    machine: Callable[[], Any]        # -> system.Machine
    errors: Any                       # system.ErrorLog
    components: Callable[[], list]    # -> list[health.ComponentState]
    quota: Callable[[], str]          # -> HTML Telegram de /quota
    views: Any = None                 # data.Views : pages « Organisation » (None : elles sont indisponibles)


def parse_day(text: str) -> date | None:
    """`AAAA-MM-JJ` -> date ; None si absent, mal formé, impossible (30 février) ou trop près des bornes du
    calendrier (la navigation ±7 jours ne doit jamais déborder)."""
    if not _ISO_DAY.fullmatch(text or ""):
        return None
    try:
        day = date.fromisoformat(text)
    except ValueError:
        return None
    return day if MINYEAR < day.year < MAXYEAR else None


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


def _hm(moment: datetime, tz: tzinfo | None = None) -> str:
    """Heure « 14h05 », ramenée au fuseau donné (celui de la fenêtre du jour) quand il y en a un."""
    if tz is not None and moment.tzinfo is not None:
        moment = moment.astimezone(tz)
    return f"{moment:%Hh%M}"


def create_app(deps: Deps, password: str, secret: str, clock: Callable[[], float] = time.time) -> web.Application:
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(HERE / "templates"), autoescape=True)
    env.filters["tg"] = lambda text: Markup(actions.safe_html(text))   # HTML Telegram filtré par safe_html
    env.globals["timedelta"] = timedelta                                 # navigation jour / semaine de l'Agenda
    env.filters["fr_day"] = fmt.day_long                                 # « mardi 27 octobre »
    env.filters["fr_day_short"] = fmt.day_short                          # « mar. 27 oct. »
    env.filters["dur"] = fmt.dur                                         # « 2 h 30 »
    env.filters["hm"] = _hm
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

    def error_page(request: web.Request, message: str = UNAVAILABLE) -> web.Response:
        """Erreur d'une page entière : dans la mise en page (menu compris), pas un fragment nu."""
        return page("error.html", request, message=message)

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

    async def section(request: web.Request, template: str, fn: Callable, key: str,
                      full_page: bool = False) -> web.Response:
        try:
            value = await blocking(fn)
            return page(template, request, **{key: value})
        except Exception:  # noqa: BLE001 - une section en panne n'empêche pas les autres
            log.exception("section web %s illisible", key)
            return error_page(request) if full_page else error_fragment(request)

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
        return await section(request, "system.html", gather, "sys", full_page=True)

    async def say(request: web.Request) -> web.Response:
        text = str((await request.post()).get("text", ""))
        try:
            replies = await blocking(actions.submit, deps.svc, text)
        except Exception:  # noqa: BLE001 - jamais de trace Python à l'écran
            log.exception("saisie web échouée")
            if text.strip():       # l'échec reste visible dans la Discussion, sous la commande tapée
                discussion.add(text.strip(), [Reply(actions.CLICK_FAILED)])
            return error_fragment(request, actions.CLICK_FAILED)
        ex = discussion.add(text.strip(), replies) if text.strip() else None
        return page("_exchange.html", request, ex=ex)

    async def click(request: web.Request) -> web.Response:
        form = await request.post()
        data = str(form.get("data", ""))
        reply = await blocking(actions.click, deps.svc, data)
        if form.get("discuss") == "1":     # réponse qui attend une suite : elle reste dans la Discussion
            discussion.add(str(form.get("label", "")).strip()[:200] or data, [reply])
        return page("_exchange.html", request, ex=actions.Exchange(time.time(), "", [reply]))

    # --- pages « Organisation » : page entière (erreur dans la mise en page) et fragment nu ---------------

    def organisation(name: str, key: str, method: str,
                     params: Callable[[web.Request], tuple] = lambda request: ()) -> tuple[Callable, Callable]:
        """Gestionnaires de `/<name>` et `/fragment/<name>` : `deps.views.<method>(*params(request))` lu dans
        l'exécuteur ; gabarits `<name>.html` (étend base.html) et `_<name>_body.html` (le corps seul)."""
        async def handle(request: web.Request, full_page: bool) -> web.Response:
            if deps.views is None:
                return error_page(request) if full_page else error_fragment(request)
            fn, args = getattr(deps.views, method), params(request)   # paramètres lus ici, pas dans l'exécuteur
            template = f"{name}.html" if full_page else f"_{name}_body.html"
            return await section(request, template, lambda: fn(*args), key, full_page=full_page)

        async def full(request: web.Request) -> web.Response:
            return await handle(request, True)

        async def fragment(request: web.Request) -> web.Response:
            return await handle(request, False)
        return full, fragment

    def agenda_params(request: web.Request) -> tuple:
        vue = request.query.get("vue", "jour")      # valeur invalide -> valeur par défaut, jamais réaffichée
        return parse_day(request.query.get("jour", "")), vue if vue in AGENDA_VUES else "jour"

    pages = {
        "agenda": organisation("agenda", "agenda", "agenda", agenda_params),
        "focus": organisation("focus", "rows", "focus"),
        "taches": organisation("taches", "tasks", "tasks", lambda request: (request.query.get("filtre", "tout"),)),
        "projets": organisation("projets", "cards", "projects"),
        "semaine": organisation("semaine", "week", "week"),
    }

    async def project(request: web.Request, full_page: bool) -> web.Response:
        fail = error_page if full_page else error_fragment
        if deps.views is None:
            return fail(request)
        try:
            value = await blocking(deps.views.project, request.match_info["id"])
            if value is None:
                return page("error.html" if full_page else "_error.html", request, status=404,
                            message=PROJECT_NOT_FOUND)
            return page("projet.html" if full_page else "_projet_body.html", request, project=value)
        except Exception:  # noqa: BLE001 - une section en panne n'empêche pas les autres
            log.exception("section web projet illisible")
            return fail(request)

    async def project_page(request: web.Request) -> web.Response:
        return await project(request, True)

    async def project_fragment(request: web.Request) -> web.Response:
        return await project(request, False)

    async def security_headers(request: web.Request, response: web.StreamResponse) -> None:
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        # /static : revalidé à chaque chargement (ETag), pour ne jamais garder l'ancien CSS/JS après un déploiement ;
        # le reste (pages, fragments, réponses d'actions) contient des données personnelles : jamais en cache
        if request.path.startswith("/static/"):
            response.headers.setdefault("Cache-Control", "no-cache")
        else:
            response.headers.setdefault("Cache-Control", "no-store")

    app = web.Application()
    app.on_response_prepare.append(security_headers)
    app.add_routes([
        web.get("/login", login_form), web.post("/login", login), web.post("/logout", protected(logout)),
        web.get("/", protected(home)), web.get("/fragment/home", protected(home_fragment)),
        web.get("/discussion", protected(discussion_page)), web.get("/systeme", protected(system_page)),
        web.post("/say", protected(say)), web.post("/click", protected(click)),
        *[route for name, (full, fragment) in pages.items()
          for route in (web.get(f"/{name}", protected(full)), web.get(f"/fragment/{name}", protected(fragment)))],
        web.get("/projets/{id}", protected(project_page)),
        web.get("/fragment/projets/{id}", protected(project_fragment)),
        web.static("/static", HERE / "static"),
    ])
    return app


class WebThread(threading.Thread):
    """Fil de la page web. Si l'adresse n'est pas encore disponible (Tailscale pas prêt au démarrage de la VM,
    port pris), il réessaie toutes les `retry_seconds` secondes, `retry_max` fois, sans jamais gêner le bot.
    `ready` est levé après le premier essai ; `serving` quand le serveur écoute."""

    def __init__(self, app: web.Application, host: str, port: int, retry_seconds: float | None = None,
                 retry_max: int | None = None) -> None:
        super().__init__(name="web", daemon=True)
        self.app, self.host, self.port = app, host, port
        self.retry_seconds = BIND_RETRY_SECONDS if retry_seconds is None else retry_seconds
        self.retry_max = BIND_RETRY_MAX if retry_max is None else retry_max
        self.ready = threading.Event()
        self.serving = threading.Event()
        self.started_ok = False
        self.retrying = False
        self.loop: asyncio.AbstractEventLoop | None = None

    async def _bind(self) -> web.AppRunner | None:
        for attempt in range(self.retry_max + 1):
            runner = web.AppRunner(self.app, access_log=None)
            try:
                await runner.setup()
                await web.TCPSite(runner, self.host, self.port).start()
            except Exception as exc:  # noqa: BLE001 - le bot continue sans la page web
                await runner.cleanup()
                if attempt < self.retry_max:
                    log.warning("page web : http://%s:%d indisponible (%s) ; nouvelle tentative dans %g s (%d/%d)",
                                self.host, self.port, exc, self.retry_seconds, attempt + 1, self.retry_max)
                    self.retrying = True
                    self.ready.set()
                    await asyncio.sleep(self.retry_seconds)
                    continue
                log.error("page web : abandon après %d essais, http://%s:%d reste indisponible (%s)",
                          attempt + 1, self.host, self.port, exc)
                self.retrying = False
                self.ready.set()
                return None
            self.port = runner.addresses[0][1]   # port réel (0 en test)
            log.info("page web : http://%s:%d", self.host, self.port)
            self.started_ok, self.retrying = True, False
            self.serving.set()
            self.ready.set()
            return runner
        return None

    def run(self) -> None:
        self.loop = loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            runner = loop.run_until_complete(self._bind())
            if runner is not None:
                try:
                    loop.run_forever()
                finally:
                    loop.run_until_complete(runner.cleanup())   # arrêt propre (loop.stop) : connexions fermées
        except Exception:  # noqa: BLE001 - jamais de trace qui remonte au bot
            log.exception("page web : arrêt inattendu")
        finally:
            self.ready.set()
            loop.close()


def start(deps: Deps | None, env: Mapping[str, str] | None = None, app: web.Application | None = None) -> WebThread | None:
    """Démarre la page web dans un fil démon ; None (bot intact) si WEB_PASSWORD manque ou si le démarrage échoue
    sans nouvelle tentative prévue. Si le premier essai échoue, le fil (vivant) réessaie en arrière-plan."""
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
    thread.ready.wait(10)            # résultat du premier essai seulement : les suivants ne bloquent pas le bot
    if thread.started_ok:
        return thread
    if thread.retrying and thread.is_alive():
        log.warning("page web : nouvelle tentative toutes les %g s en arrière-plan", thread.retry_seconds)
        return thread
    return None
