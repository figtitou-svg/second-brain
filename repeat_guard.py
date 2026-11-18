"""Anti-répétition : un même message envoyé en boucle (bug comme la boucle « 🔀 … passe avant » du 18/11) est
bloqué au 3e envoi identique en 2 h, et l'utilisateur est prévenu UNE fois. Le blocage dure tant que le message
continue d'arriver (fenêtre glissante) ; la panne est signalée à la section Système (composant « repeat »)."""
from __future__ import annotations

import logging
import re
import threading
from collections import deque
from datetime import datetime, timedelta
from typing import Any, Callable
from zoneinfo import ZoneInfo

log = logging.getLogger("repeat_guard")
TZ = ZoneInfo("Europe/Paris")
LIMIT = 3                       # au 3e envoi identique…
WINDOW = timedelta(hours=2)     # …dans les 2 dernières heures : bloqué
Notify = Callable[..., Any]


def _key(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


class RepeatGuard:
    def __init__(self, send: Notify, limit: int = LIMIT, window: timedelta = WINDOW,
                 now_fn: Callable[[], datetime] | None = None) -> None:
        self.send, self.limit, self.window = send, limit, window
        self._now = now_fn or (lambda: datetime.now(TZ))
        self._sent: dict[str, deque[datetime]] = {}
        self._blocked: dict[str, int] = {}          # message bloqué -> nombre d'envois retenus
        self._lock = threading.Lock()
        self.on_block: Callable[[str, int], None] | None = None   # ex. health : panne visible dans Système
        self.on_clear: Callable[[], None] | None = None           # plus aucun message bloqué : panne levée

    def __call__(self, text: str, buttons: Any = None) -> Any:
        key, now = _key(text), self._now()
        with self._lock:
            times = self._sent.setdefault(key, deque())
            while times and now - times[0] > self.window:
                times.popleft()
            cleared = False
            for k in [k for k in self._blocked if k != key and not any(now - t <= self.window for t in self._sent.get(k, ()))]:
                del self._blocked[k]                # cette boucle s'est arrêtée
                cleared = True
            if not times and key in self._blocked:
                del self._blocked[key]              # la boucle s'est arrêtée : le message peut repartir
                cleared = True
            cleared = cleared and not self._blocked
            times.append(now)
            blocked = len(times) >= self.limit
            first_block = blocked and key not in self._blocked
            if blocked:
                self._blocked[key] = self._blocked.get(key, 0) + 1
            if len(self._sent) > 500:               # mémoire bornée : on oublie les messages anciens
                for k in [k for k, v in self._sent.items() if not v or now - v[-1] > self.window][:250]:
                    self._sent.pop(k, None)
        if cleared and self.on_clear:
            try:
                self.on_clear()
            except Exception:  # noqa: BLE001
                log.warning("levée de la panne de répétition impossible", exc_info=True)
        if not blocked:
            return self.send(text, buttons)
        log.error("message répété bloqué (%d envois en %s) : %s", len(times), self.window, key[:120])
        if first_block:
            short = key if len(key) <= 160 else key[:157] + "…"
            self.send("⚠️ <b>Message répété bloqué</b>\nLe même message est parti "
                      f"{self.limit - 1} fois en {int(self.window.total_seconds() // 3600)} h ; je ne l'envoie plus "
                      "tant qu'il revient. C'est sûrement un bug : il est signalé dans Système.\n"
                      f"<i>{_strip(short)}</i>", None)
            if self.on_block:
                try:
                    self.on_block(key, len(times))
                except Exception:  # noqa: BLE001 - le signalement ne doit jamais casser l'envoi
                    log.warning("signalement de la répétition impossible", exc_info=True)
        return True


def _strip(html_text: str) -> str:
    """Le message cité sans ses balises (il est remis en italique tel quel, échappé)."""
    from html import escape, unescape
    return escape(unescape(re.sub(r"<[^>]+>", "", html_text)))
