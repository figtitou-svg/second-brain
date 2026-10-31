# tests/test_backup.py
from __future__ import annotations

import importlib.util
import sqlite3
import tarfile
from datetime import datetime
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location("backup", Path(__file__).resolve().parent.parent / "deploy" / "backup.py")
backup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(backup)

NOW = datetime(2026, 10, 31, 3, 10)


def make_dbs(data: Path) -> None:
    data.mkdir(parents=True, exist_ok=True)
    for name in backup.DATABASES:
        con = sqlite3.connect(data / name)
        con.execute("CREATE TABLE t (x TEXT)")
        con.executemany("INSERT INTO t VALUES (?)", [("é",), ("b",)])
        con.commit()
        con.close()


def test_backup_contains_both_readable_databases(tmp_path):
    data, dest = tmp_path / "data", tmp_path / "sauv"
    make_dbs(data)
    archive = backup.run_backup(data, dest, NOW)
    assert archive.name == "2026-10-31.tgz"
    out = tmp_path / "out"
    with tarfile.open(archive) as tar:
        assert sorted(tar.getnames()) == ["coach.sqlite", "memory.sqlite"]
        tar.extractall(out, filter="data")
    for name in backup.DATABASES:
        con = sqlite3.connect(out / name)
        assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert con.execute("SELECT count(*) FROM t").fetchone()[0] == 2
        con.close()


def test_backup_is_consistent_while_the_bot_holds_a_write_transaction(tmp_path):
    data, dest = tmp_path / "data", tmp_path / "sauv"
    make_dbs(data)
    writer = sqlite3.connect(data / "coach.sqlite", isolation_level=None)
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("INSERT INTO t VALUES ('en cours')")
    try:
        archive = backup.run_backup(data, dest, NOW)
    finally:
        writer.execute("ROLLBACK")
        writer.close()
    backup.verify(archive)


def test_missing_database_leaves_nothing_and_keeps_old_backups(tmp_path):
    data, dest = tmp_path / "data", tmp_path / "sauv"
    make_dbs(data)
    old = backup.run_backup(data, dest, datetime(2026, 10, 30, 3, 10))
    (data / "memory.sqlite").unlink()
    with pytest.raises(FileNotFoundError):
        backup.run_backup(data, dest, NOW)
    assert sorted(p.name for p in dest.iterdir()) == [old.name]


def test_prune_keeps_the_14_most_recent_and_ignores_other_files(tmp_path):
    data, dest = tmp_path / "data", tmp_path / "sauv"
    make_dbs(data)
    dest.mkdir()
    (dest / "notes.txt").write_text("à garder")
    for day in range(1, 21):
        (dest / f"2026-10-{day:02d}.tgz").write_bytes(b"x")
    backup.run_backup(data, dest, NOW)                   # 21e archive
    names = sorted(p.name for p in dest.glob("????-??-??.tgz"))
    assert len(names) == 14 and names[-1] == "2026-10-31.tgz" and names[0] == "2026-10-08.tgz"
    assert (dest / "notes.txt").exists()


def test_verify_rejects_a_corrupt_archive(tmp_path):
    bad = tmp_path / "2026-10-31.tgz"
    bad.write_bytes(b"pas une archive")
    with pytest.raises(Exception):
        backup.verify(bad)


def test_main_alerts_once_on_failure_and_stays_quiet_on_success(tmp_path):
    data, dest = tmp_path / "data", tmp_path / "sauv"
    sent: list[str] = []
    env = {"BACKUP_DATA": str(data), "BACKUP_DIR": str(dest)}
    assert backup.main([], env, NOW, sent.append) == 1            # bases absentes
    assert len(sent) == 1 and "sauvegarde" in sent[0].lower()
    make_dbs(data)
    sent.clear()
    assert backup.main([], env, NOW, sent.append) == 0 and sent == []
    assert backup.main(["--check"], env, NOW, sent.append) == 0
    (dest / "2026-10-31.tgz").write_bytes(b"corrompu")
    assert backup.main(["--check"], env, NOW, sent.append) == 1


def test_alert_without_telegram_credentials_does_not_raise(tmp_path):
    env = {"BACKUP_DATA": str(tmp_path / "nope"), "BACKUP_DIR": str(tmp_path / "s")}
    assert backup.main([], env, NOW) == 1                          # send par défaut : pas de jeton -> ignoré
