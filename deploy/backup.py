"""Sauvegarde nocturne des bases du bot (lancée par deploy/second-brain-backup.timer sur la VM).

    python deploy/backup.py            # sauvegarde, vérifie, purge (14 gardées), prévient par Telegram en cas d'échec
    python deploy/backup.py --check [archive]   # ouvre la dernière sauvegarde (ou celle donnée) et la vérifie

Copie cohérente par l'API de sauvegarde de SQLite (sûre pendant que le bot écrit), sans dépendance au bot.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import tarfile
import tempfile
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Callable, Mapping

DATABASES = ("coach.sqlite", "memory.sqlite")
KEEP = 14
ROOT = Path(__file__).resolve().parent.parent
_ARCHIVE = re.compile(r"\d{4}-\d{2}-\d{2}\.tgz")


def snapshot(src: Path, dest: Path) -> None:
    """Copie cohérente d'une base SQLite ; ne crée jamais une base vide à la place d'une base absente."""
    if not src.exists():
        raise FileNotFoundError(f"base introuvable : {src}")
    source = sqlite3.connect(str(src), timeout=30)
    try:
        target = sqlite3.connect(str(dest))
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()


def archives(dest_dir: Path) -> list[Path]:
    """Les archives AAAA-MM-JJ.tgz (chiffres seulement), de la plus ancienne à la plus récente."""
    return sorted(p for p in dest_dir.glob("*.tgz") if _ARCHIVE.fullmatch(p.name))


def prune(dest_dir: Path, keep: int = KEEP) -> None:
    """Ne touche qu'aux archives AAAA-MM-JJ.tgz ; garde les `keep` plus récentes."""
    found = archives(dest_dir)
    for old in found[:-keep] if keep > 0 else found:
        old.unlink()


def verify(archive: Path) -> None:
    """Ouvre l'archive : chaque base doit passer `PRAGMA integrity_check` et contenir au moins une table."""
    with tempfile.TemporaryDirectory() as tmp:
        with tarfile.open(archive) as tar:
            names = tar.getnames()
            if sorted(names) != sorted(DATABASES):
                raise RuntimeError(f"contenu inattendu : {names}")
            tar.extractall(tmp, filter="data")
        for name in DATABASES:
            con = sqlite3.connect(str(Path(tmp) / name))
            try:
                if con.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise RuntimeError(f"{name} : contrôle d'intégrité échoué")
                if con.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0] == 0:
                    raise RuntimeError(f"{name} : aucune table")
            finally:
                con.close()


def run_backup(data_dir: Path, dest_dir: Path, now: datetime, keep: int = KEEP) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    try:
        dest_dir.chmod(0o700)                # copies des bases : lisibles par le seul compte du bot
    except OSError:
        pass
    final = dest_dir / f"{now:%Y-%m-%d}.tgz"
    part = dest_dir / f"{now:%Y-%m-%d}.tgz.part"
    try:
        with tempfile.TemporaryDirectory() as tmp:
            for name in DATABASES:
                snapshot(data_dir / name, Path(tmp) / name)
            with tarfile.open(part, "w:gz") as tar:
                for name in DATABASES:
                    tar.add(Path(tmp) / name, arcname=name)
        verify(part)
        part.replace(final)
    finally:
        part.unlink(missing_ok=True)         # jamais d'archive partielle laissée en place
    prune(dest_dir, keep)                    # seulement après une sauvegarde réussie
    return final


def telegram_alert(env: Mapping[str, str], text: str) -> None:
    """Message Telegram ; sans jeton ou sans réseau, on n'ajoute pas une seconde erreur."""
    token, chat = env.get("TELEGRAM_BOT_TOKEN", "").strip(), env.get("TELEGRAM_ALLOWED_USER_ID", "").strip()
    if not token or not chat:
        return
    body = json.dumps({"chat_id": chat, "text": text}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=15).read()
    except Exception:  # noqa: BLE001
        pass


def main(argv: list[str], env: Mapping[str, str], now: datetime | None = None,
         send: Callable[[str], None] | None = None) -> int:
    now = now or datetime.now()
    data_dir = Path(env.get("BACKUP_DATA") or ROOT / "data")
    dest_dir = Path(env.get("BACKUP_DIR") or Path.home() / "sauvegardes")
    send = send or (lambda text: telegram_alert(env, text))
    if argv and argv[0] == "--check":
        target = Path(argv[1]) if len(argv) > 1 else max(archives(dest_dir), default=None)
        try:
            if target is None:
                raise FileNotFoundError("aucune sauvegarde")
            verify(target)
        except Exception as exc:  # noqa: BLE001
            print(f"ÉCHEC : {exc}")
            return 1
        print(f"OK : {target}")
        return 0
    try:
        archive = run_backup(data_dir, dest_dir, now)
    except Exception as exc:  # noqa: BLE001 - le motif part par Telegram, le code de sortie par systemd
        send(f"⚠️ La sauvegarde de la nuit a échoué : {type(exc).__name__} : {exc}")
        print(f"ÉCHEC : {exc}", file=sys.stderr)
        return 1
    print(f"OK : {archive}")
    return 0


if __name__ == "__main__":
    try:
        from dotenv import dotenv_values
        merged = {**dotenv_values(ROOT / ".env"), **os.environ}
    except ImportError:
        merged = dict(os.environ)
    sys.exit(main(sys.argv[1:], merged))
