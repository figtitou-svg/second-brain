"""Journal des messages que le bot envoie de lui-même : enveloppe l'envoi, n'y change rien, ne le bloque jamais."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Callable
from zoneinfo import ZoneInfo

log = logging.getLogger("web.notifylog")
TZ = ZoneInfo("Europe/Paris")


def _rows(buttons: Any) -> list[list[tuple[str, str]]]:
    if not buttons:
        return []
    rows = buttons if isinstance(buttons[0], (list, tuple)) and buttons[0] and isinstance(buttons[0][0], (list, tuple)) \
        else [buttons]
    return [[(str(label), str(cb)) for label, cb in row] for row in rows]


def logged(notify: Callable[..., Any], get_db: Callable[[], Any], translate: Callable[[str], str],
           clock: Callable[[], datetime] | None = None) -> Callable[..., Any]:
    now = clock or (lambda: datetime.now(TZ))

    def record(text: str, buttons: Any, delivered: bool) -> None:
        try:
            db = get_db()
            if db is None:
                return                  # avant l'ouverture de la base (alerte de démarrage) : rien à journaliser
            shown = [[(label, translate(cb)) for label, cb in row] for row in _rows(buttons)]
            db.log_notification(text, shown, delivered, now())
        except Exception:  # noqa: BLE001 - le journal ne doit JAMAIS gêner l'envoi
            log.warning("journal des notifications : enregistrement impossible", exc_info=True)

    def send(text: str, buttons: Any = None) -> Any:
        try:
            result = notify(text, buttons)
        except Exception:
            record(text, buttons, False)
            raise
        record(text, buttons, bool(result) if result is not None else True)   # None : réputé envoyé
        return result
    return send
