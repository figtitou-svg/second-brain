"""Alertes Telegram quand un composant ne marche plus (sans spam) et quand il se rétablit.

Chaque composant (gmail, drive, notion, coach, bot...) signale ses réussites/échecs. Une alerte part après
`threshold` échecs consécutifs, puis au plus une fois toutes les `cooldown_hours` ; un message « rétabli »
suit le premier succès. L'état est gardé dans data/health.json (survit aux redémarrages).
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

log = logging.getLogger("health")
TZ = ZoneInfo("Europe/Paris")
STATE = Path("data") / "health.json"
HEARTBEAT = Path("data") / "heartbeat"
RUNNING = Path("data") / "running"
LABELS = {"gmail": "Gmail", "drive": "Drive", "notion": "Notion", "coach": "Agenda / relances",
          "llm": "Modèles IA", "bot": "Bot Telegram", "google_auth": "Autorisation Google",
          "projects": "Assistant de projet", "pwatch": "Surveillance des projets"}


def describe(exc: BaseException) -> tuple[str, str, int]:
    """(clé de composant imposée ou '', explication lisible, seuil d'échecs avant alerte)."""
    name, text = type(exc).__name__, str(exc)
    blob = f"{name} {text}"
    if "invalid_grant" in blob or name == "RefreshError":
        return ("google_auth", "L'autorisation Google a expiré ou a été révoquée. Il faut refaire l'authentification "
                "(Gmail, Calendar et Drive sont à l'arrêt tant que ce n'est pas fait).", 1)
    if name in ("LLMUnavailableError", "ChainExhausted", "PrivateModelUnavailableError") or "ChainExhausted" in blob:
        return ("llm", "Aucun modèle IA n'a répondu (quota épuisé ou service saturé). Vérifie /quota.", 2)
    if "insufficient" in blob.lower() and ("scope" in blob.lower() or "permission" in blob.lower()):
        return ("google_auth", "Il manque une autorisation Google (nouvelle fonction). Refaire l'authentification "
                "Google pour l'accorder.", 1)
    if "timeRangeTooLong" in blob:
        return ("", "Google Calendar a refusé une plage de dates trop longue.", 2)
    if name == "HTTPStatusError" and ("401" in text or "403" in text):
        return ("", "Accès refusé (token invalide ou ressource non partagée avec l'intégration).", 1)
    if name == "HttpError" and "403" in text and ("accessNotConfigured" in text or "has not been used" in text):
        return ("", "Une API Google n'est pas activée dans le projet Google Cloud.", 1)
    return ("", f"{name} : {text[:160]}", 2)


class Health:
    def __init__(self, notify: Callable[[str, Any], None], path: Path = STATE, threshold: int = 2,
                 cooldown_hours: float = 6, now_fn: Callable[[], datetime] | None = None) -> None:
        self.notify, self.path, self.threshold = notify, path, threshold
        self.cooldown = timedelta(hours=cooldown_hours)
        self._now = now_fn or (lambda: datetime.now(TZ))
        self._lock = threading.Lock()
        try:
            self._state: dict[str, dict] = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            self._state = {}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._state), encoding="utf-8")

    def ok(self, component: str) -> None:
        with self._lock:
            st = self._state.pop(component, None)
            if st is None:
                return
            self._save()
            alerted = bool(st.get("alerted_at"))
        if alerted:
            self.notify(f"✅ <b>{LABELS.get(component, component)}</b> fonctionne de nouveau.", None)

    def fail(self, component: str, exc: BaseException) -> None:
        override, why, threshold = describe(exc)
        component = override or component
        log.warning("échec %s : %s", component, why)
        with self._lock:
            st = self._state.setdefault(component, {"fails": 0, "alerted_at": None})
            st["fails"] += 1
            st["last"] = why
            now = self._now()
            due = st["alerted_at"] is None or now - datetime.fromisoformat(st["alerted_at"]) >= self.cooldown
            send = st["fails"] >= max(threshold, 1) and due
            if send:
                st["alerted_at"] = now.isoformat()
            self._save()
            fails = st["fails"]
        if send:
            self.notify(f"⚠️ <b>{LABELS.get(component, component)} : problème</b>\n{_html(why)}"
                        + (f"\n<i>{fails} échecs d'affilée</i>" if fails > 1 else ""), None)


def _html(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def guard(health: Health, component: str, fn: Callable[[], Any]) -> Callable[[], None]:
    """Enveloppe d'un job périodique : n'interrompt jamais le planificateur, signale succès/échecs."""
    def run() -> None:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            log.exception("job %s en échec", component)
            health.fail(component, exc)
        else:
            health.ok(component)
    return run


# --- démarrage : arrêt inattendu / coupure --------------------------------------------------

def startup_check(notify: Callable[[str, Any], None], now: datetime | None = None,
                  running: Path = RUNNING, heartbeat: Path = HEARTBEAT, gap_minutes: int = 10) -> None:
    """Prévient si le service a redémarré après un arrêt inattendu ou une longue coupure."""
    now = now or datetime.now(TZ)
    crashed = running.exists()
    last = None
    try:
        last = datetime.fromisoformat(heartbeat.read_text(encoding="utf-8").strip())
    except Exception:
        pass
    running.parent.mkdir(parents=True, exist_ok=True)
    running.write_text(now.isoformat(), encoding="utf-8")
    if last is not None and now - last >= timedelta(minutes=gap_minutes):
        mins = int((now - last).total_seconds() // 60)
        notify(f"🔄 <b>Le bot vient de redémarrer</b> après environ {mins} min d'arrêt.", None)
    elif crashed:
        notify("🔄 <b>Le bot a redémarré après un arrêt inattendu.</b> Si ça se répète : "
               "<code>journalctl -u second-brain</code> sur la VM.", None)


def beat(heartbeat: Path = HEARTBEAT, now: datetime | None = None) -> None:
    heartbeat.parent.mkdir(parents=True, exist_ok=True)
    heartbeat.write_text((now or datetime.now(TZ)).isoformat(), encoding="utf-8")


def clean_exit(running: Path = RUNNING) -> None:
    running.unlink(missing_ok=True)


_instance: Health | None = None


def set_instance(h: Health) -> None:
    global _instance
    _instance = h


def report_exception(component: str, exc: BaseException) -> None:
    """Pour le code qui n'a pas accès à l'instance (gestionnaire d'erreurs du bot)."""
    if _instance is not None:
        _instance.fail(component, exc)
