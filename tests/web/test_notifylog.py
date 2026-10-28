# tests/web/test_notifylog.py
from __future__ import annotations

import sqlite3
from datetime import timedelta

import pytest

from coach.db import Database
from tests.coach.conftest import dt
from web import notifylog

NOW = dt(2026, 10, 28, 10, 27)
TR = lambda cb: cb.replace("done:", "ok:", 1)     # noqa: E731 - comme run._tr, version minimale


def make(raw_result=True):
    db, sent = Database(":memory:"), []

    def raw(text, buttons=None):
        sent.append((text, buttons))
        return raw_result
    return db, sent, notifylog.logged(raw, lambda: db, TR, clock=lambda: NOW)


def test_db_roundtrip_order_since_and_purge():
    db = Database(":memory:")
    db.log_notification("vieux", [], True, NOW - timedelta(days=40))
    db.log_notification("hier", [[("Oui", "ok:1")]], True, NOW - timedelta(days=1))
    db.log_notification("maintenant « é »", [[("Non", "nf:1"), ("Plus tard", "rep:1")]], False, NOW)
    rows = db.list_notifications()
    assert [r.text for r in rows] == ["maintenant « é »", "hier", "vieux"]               # plus récent d'abord
    assert rows[0].buttons == [[("Non", "nf:1"), ("Plus tard", "rep:1")]] and rows[0].delivered is False
    assert rows[1].sent_at == NOW - timedelta(days=1) and rows[1].delivered is True
    assert [r.text for r in db.list_notifications(since=NOW - timedelta(days=2))] == ["maintenant « é »", "hier"]
    assert [r.text for r in db.list_notifications(limit=1)] == ["maintenant « é »"]
    assert db.purge_notifications(NOW - timedelta(days=30)) == 1
    assert [r.text for r in db.list_notifications()] == ["maintenant « é »", "hier"]


def test_logged_notify_logs_translated_buttons():
    db, sent, notify = make()
    assert notify("<b>Salut</b>", [[("Fait", "done:b1"), ("Reporter", "rep:b1")]]) is True
    assert sent == [("<b>Salut</b>", [[("Fait", "done:b1"), ("Reporter", "rep:b1")]])]    # l'envoi reçoit l'original
    (row,) = db.list_notifications()
    assert row.buttons == [[("Fait", "ok:b1"), ("Reporter", "rep:b1")]] and row.delivered and row.sent_at == NOW


def test_flat_button_list_is_normalised_to_rows_and_none_is_empty():
    db, _, notify = make()
    notify("a", [("Oui", "done:1"), ("Non", "nf:1")])
    notify("b")
    notify("c", None)
    rows = {r.text: r.buttons for r in db.list_notifications()}
    assert rows["a"] == [[("Oui", "ok:1"), ("Non", "nf:1")]] and rows["b"] == [] and rows["c"] == []


def test_failed_send_is_logged_as_not_delivered_and_result_passes_through():
    db, _, notify = make(raw_result=False)
    assert notify("raté") is False
    assert [r.delivered for r in db.list_notifications()] == [False]


def test_exception_from_send_is_logged_then_reraised():
    db = Database(":memory:")

    def boom(text, buttons=None):
        raise RuntimeError("réseau")
    notify = notifylog.logged(boom, lambda: db, TR, clock=lambda: NOW)
    with pytest.raises(RuntimeError):
        notify("x")
    assert [(r.text, r.delivered) for r in db.list_notifications()] == [("x", False)]


def test_logging_failure_never_blocks_sending():
    sent = []

    def raw(text, buttons=None):
        sent.append(text)
        return True

    def no_db():
        raise sqlite3.OperationalError("database is locked")
    notify = notifylog.logged(raw, no_db, TR, clock=lambda: NOW)
    assert notify("part quand même", [[("a", "b")]]) is True and sent == ["part quand même"]
    db = Database(":memory:")
    bad = notifylog.logged(raw, lambda: db, lambda cb: 1 / 0, clock=lambda: NOW)       # traduction qui plante
    assert bad("encore", [[("a", "b")]]) is True and sent[-1] == "encore"


def test_list_proposals_filters_by_state():
    db = Database(":memory:")
    db.set_proposal("t1", "p1", None, "proposed")
    db.set_proposal("t2", "p1", "m1", "lot")
    assert [p["task_id"] for p in db.list_proposals()] == ["t1"]
    assert [p["task_id"] for p in db.list_proposals("lot")] == ["t2"]


def test_existing_database_without_the_table_is_migrated(tmp_path):
    path = tmp_path / "old.sqlite"
    Database(path).close()
    con = sqlite3.connect(path)
    con.execute("DROP TABLE notifications_log")
    con.commit()
    con.close()
    db = Database(path)                                  # redémarrage : la table est recréée
    db.log_notification("ok", [], True, NOW)
    assert [r.text for r in db.list_notifications()] == ["ok"]


def test_dst_change_orders_by_instant():
    db = Database(":memory:")
    db.log_notification("avant", [], True, dt(2026, 10, 25, 2, 30))       # 02:30 Paris, avant le passage à l'heure d'hiver
    db.log_notification("apres", [], True, dt(2026, 10, 25, 3, 10))       # 03:10 Paris, une heure plus tard en UTC
    assert [r.text for r in db.list_notifications()] == ["apres", "avant"]
    assert [r.text for r in db.list_notifications(since=dt(2026, 10, 25, 3, 0))] == ["apres"]


def test_db_not_created_yet_then_set_like_run_py():
    holder, sent = {"db": None}, []

    def raw(text, buttons=None):
        sent.append(text)
        return True
    notify = notifylog.logged(raw, lambda: holder["db"], TR, clock=lambda: NOW)
    assert notify("alerte de démarrage") is True            # base pas encore créée : envoyé, non journalisé
    holder["db"] = Database(":memory:")
    assert notify("après") is True
    assert sent == ["alerte de démarrage", "après"]
    assert [r.text for r in holder["db"].list_notifications()] == ["après"]
