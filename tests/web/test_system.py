from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import health
from web import system

TZ = ZoneInfo("Europe/Paris")


def fake_proc(tmp_path):
    (tmp_path / "meminfo").write_text(
        "MemTotal:  976896 kB\nMemAvailable:  315392 kB\nSwapTotal: 1048572 kB\nSwapFree:  868352 kB\n")
    (tmp_path / "loadavg").write_text("0.15 0.05 0.01 1/150 1234\n")
    (tmp_path / "uptime").write_text("9330000.12 18000000.00\n")
    (tmp_path / "self").mkdir()
    (tmp_path / "self" / "status").write_text("Name:\tpython\nVmRSS:\t  292472 kB\n")
    return tmp_path


def test_machine_reads_proc(tmp_path):
    m = system.machine(fake_proc(tmp_path), str(tmp_path))
    assert m.mem_total_mb == 954 and m.mem_used_mb == 646
    assert m.swap_total_mb == 1024 and m.swap_used_mb == 176
    assert m.load1 == 0.15 and m.uptime_s == 9330000
    assert m.bot_rss_mb == 286
    assert m.disk_total_gb > 0 and 0 <= m.disk_used_gb <= m.disk_total_gb


def test_machine_without_proc_gives_none(tmp_path):
    m = system.machine(tmp_path / "absent", str(tmp_path))
    assert m.mem_total_mb is None and m.load1 is None and m.uptime_s is None and m.bot_rss_mb is None
    assert m.disk_total_gb > 0          # le disque se lit partout


def test_error_log_groups_and_caps():
    errors = system.ErrorLog(limit=3)
    log = logging.getLogger("test.web.system")
    log.addHandler(errors)
    log.propagate = False
    try:
        for _ in range(4):
            log.warning("Conflict: terminated by other getUpdates request")
        log.info("ignoré : niveau trop bas")
        for i in range(4):
            log.error("panne %d", i)
    finally:
        log.removeHandler(errors)
    entries = errors.entries()
    assert len(entries) == 3                                   # plafond respecté
    assert [e.message for e in entries] == ["panne 3", "panne 2", "panne 1"]   # plus récentes d'abord
    errors2 = system.ErrorLog()
    log.addHandler(errors2)
    try:
        log.warning("même message")
        log.warning("même message")
    finally:
        log.removeHandler(errors2)
    (e,) = errors2.entries()
    assert e.count == 2 and e.level == "WARNING" and e.logger == "test.web.system"


def test_health_snapshot(tmp_path):
    t = [datetime(2026, 10, 27, 10, 0, tzinfo=TZ)]
    h = health.Health(lambda text, b=None: None, path=tmp_path / "h.json", now_fn=lambda: t[0])
    h.ok("gmail")
    h.fail("notion", RuntimeError("délai dépassé"))
    snap = {c.component: c for c in h.snapshot()}
    assert snap["gmail"].ok and snap["gmail"].last_ok == t[0] and snap["gmail"].fails == 0
    assert not snap["notion"].ok and snap["notion"].fails == 1 and "délai" in snap["notion"].last_error
    assert snap["drive"].ok and snap["drive"].last_ok is None   # jamais exécuté depuis le démarrage
    assert snap["gmail"].label == "Gmail"
