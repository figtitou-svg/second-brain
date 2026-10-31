"""Direct : un petit « bus » entre les fils qui envoient (bot, actions) et les pages ouvertes (flux /events)."""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

log = logging.getLogger("web.live")
MAX_STREAMS = 4         # onglets simultanés : au-delà, le plus ancien est fermé
QUEUE_SIZE = 20         # événements gardés par flux ; un abonné qui ne lit pas est coupé, il se reconnectera


class Stream:
    """Un abonné (une page ouverte). `None` dans la file = fin du flux ; `closed` : plus rien n'y est ajouté."""

    def __init__(self) -> None:
        self.queue: asyncio.Queue = asyncio.Queue(QUEUE_SIZE)
        self.closed: bool = False


class Bus:
    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._streams: list[Stream] = []
        self._listeners: list[Callable[[str, dict], None]] = []

    def attach(self, loop: asyncio.AbstractEventLoop) -> None:
        """Appelé au démarrage du serveur : la boucle qui sert les flux."""
        self._loop = loop

    def add_listener(self, fn: Callable[[str, dict], None]) -> None:
        """Écouteur synchrone, appelé dans le fil qui publie, avant la diffusion (historique de la Discussion)."""
        self._listeners.append(fn)

    def open(self) -> Stream:
        """À appeler sur la boucle du serveur."""
        stream = Stream()
        self._streams.append(stream)
        while len(self._streams) > MAX_STREAMS:
            oldest = self._streams.pop(0)
            self._put(oldest, None)
            oldest.closed = True
        return stream

    def close(self, stream: Stream) -> None:
        if stream in self._streams:
            self._streams.remove(stream)

    def publish(self, kind: str, data: dict[str, Any] | None = None) -> None:
        """Depuis n'importe quel fil. Ne lève jamais ; ne fait rien (hors écouteurs) sans abonné."""
        data = data or {}
        for fn in list(self._listeners):
            try:
                fn(kind, data)
            except Exception:  # noqa: BLE001
                log.warning("direct : écouteur en panne", exc_info=True)
        loop = self._loop
        if loop is None or not self._streams:
            return
        try:
            loop.call_soon_threadsafe(self._fanout, kind, data)
        except RuntimeError:        # boucle fermée (arrêt du serveur)
            pass
        except Exception:  # noqa: BLE001
            log.warning("direct : diffusion impossible", exc_info=True)

    def _fanout(self, kind: str, data: dict) -> None:
        for stream in list(self._streams):
            self._put(stream, (kind, data))

    @staticmethod
    def _put(stream: Stream, item: Any) -> None:
        if stream.closed:
            return                                # déjà coupé : rien après la fin
        try:
            stream.queue.put_nowait(item)
        except asyncio.QueueFull:
            while not stream.queue.empty():
                stream.queue.get_nowait()
            stream.queue.put_nowait(None)         # abonné trop lent : coupé, le navigateur se reconnecte
            stream.closed = True
