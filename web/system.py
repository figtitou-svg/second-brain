"""Section Système : état de la machine (lu dans /proc, sans dépendance) et erreurs récentes du journal."""
from __future__ import annotations

import logging
import shutil
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

STARTED = time.time()   # démarrage du processus du bot


@dataclass(frozen=True)
class Machine:
    mem_total_mb: int | None
    mem_used_mb: int | None
    swap_total_mb: int | None
    swap_used_mb: int | None
    load1: float | None
    uptime_s: int | None
    bot_rss_mb: int | None
    bot_uptime_s: int
    disk_total_gb: float
    disk_used_gb: float


def _kb_fields(path: Path) -> dict[str, int]:
    out: dict[str, int] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            key, _, rest = line.partition(":")
            parts = rest.split()
            if parts and parts[0].isdigit():
                out[key.strip()] = int(parts[0])
    except OSError:
        pass
    return out


def _first_float(path: Path) -> float | None:
    try:
        return float(path.read_text(encoding="utf-8").split()[0])
    except (OSError, ValueError, IndexError):
        return None


def _mb(kb: int) -> int:
    return round(kb / 1024)


def machine(proc: Path = Path("/proc"), disk_path: str = "/") -> Machine:
    """Valeurs illisibles (Windows, conteneur) : None, jamais d'exception."""
    mem = _kb_fields(proc / "meminfo")
    total, avail = mem.get("MemTotal"), mem.get("MemAvailable")
    swap_total, swap_free = mem.get("SwapTotal"), mem.get("SwapFree")
    up = _first_float(proc / "uptime")
    rss = _kb_fields(proc / "self" / "status").get("VmRSS")
    du = shutil.disk_usage(disk_path)
    return Machine(
        mem_total_mb=_mb(total) if total is not None else None,
        mem_used_mb=_mb(total - avail) if total is not None and avail is not None else None,
        swap_total_mb=_mb(swap_total) if swap_total is not None else None,
        swap_used_mb=_mb(swap_total - swap_free) if swap_total is not None and swap_free is not None else None,
        load1=_first_float(proc / "loadavg"),
        uptime_s=int(up) if up is not None else None,
        bot_rss_mb=_mb(rss) if rss is not None else None,
        bot_uptime_s=int(time.time() - STARTED),
        disk_total_gb=round(du.total / 1e9, 1),
        disk_used_gb=round(du.used / 1e9, 1))


@dataclass
class ErrorEntry:
    logger: str
    level: str
    message: str
    count: int
    first: float
    last: float


class ErrorLog(logging.Handler):
    """Garde en mémoire les derniers messages WARNING+ ; un même message est compté, pas répété."""

    def __init__(self, limit: int = 50) -> None:
        super().__init__(level=logging.WARNING)
        self.limit = limit
        self._items: OrderedDict[tuple[str, str], ErrorEntry] = OrderedDict()
        self._mutex = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()[:200]
        except Exception:  # noqa: BLE001 - un message mal formé ne doit pas casser la journalisation
            msg = str(record.msg)[:200]
        key = (record.name, msg)
        with self._mutex:
            e = self._items.pop(key, None)
            if e is None:
                e = ErrorEntry(record.name, record.levelname, msg, 0, record.created, record.created)
            e.count += 1
            e.last = record.created
            self._items[key] = e
            while len(self._items) > self.limit:
                self._items.popitem(last=False)

    def entries(self) -> list[ErrorEntry]:
        with self._mutex:
            return list(reversed(self._items.values()))   # la plus récemment vue d'abord (l'ordre d'insertion = récence)
