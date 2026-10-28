from __future__ import annotations

from datetime import datetime

import pytest

import memory


@pytest.fixture
def mem(tmp_path, monkeypatch):
    monkeypatch.setattr(memory, "DB_PATH", tmp_path / "memory.sqlite")
    monkeypatch.setattr(memory, "_conn", None)
    yield memory
    if memory._conn is not None:
        memory._conn.close()
    monkeypatch.setattr(memory, "_conn", None)


def seed(mem):
    a = mem.add_note("Le code du garage est 4721.\nÀ ne pas perdre.", "private", "telegram")
    b = mem.add_mail("Bonjour, rendez-vous mardi pour le stage chez Acme.", "Stage Acme", "gmail:42")
    c = mem.add_note("Idée : relire le rapport de projet IA.", "public", "telegram")
    return a, b, c


def test_recent_docs_newest_first_with_short_flat_excerpt(mem):
    a, b, c = seed(mem)
    long = mem.add_note("mot " * 400, "private", "telegram")
    docs = mem.recent_docs()
    assert [d["id"] for d in docs] == [long, c, b, a]
    assert all(isinstance(d["created_at"], datetime) for d in docs)
    assert len(docs[0]["excerpt"]) <= 240 and "\n" not in docs[-1]["excerpt"]
    assert docs[-1]["sensitivity"] == "private" and docs[1]["sensitivity"] == "public"
    assert [d["id"] for d in mem.recent_docs(limit=2)] == [long, c]
    assert [d["id"] for d in mem.recent_docs(kind="mail")] == [b]


def test_get_doc_returns_full_body_or_none(mem):
    a, *_ = seed(mem)
    d = mem.get_doc(a)
    assert d["body"].startswith("Le code du garage") and "À ne pas perdre." in d["body"] and d["id"] == a
    assert mem.get_doc("inconnu") is None


def test_search_docs_finds_words_without_accents_and_returns_excerpt(mem):
    a, b, c = seed(mem)
    assert [d["id"] for d in mem.search_docs("garage")] == [a]
    assert b in [d["id"] for d in mem.search_docs("stage acme")]
    assert [d["id"] for d in mem.search_docs("idee")] == [c]                    # sans accent, comme l'index
    assert "garage" in mem.search_docs("garage")[0]["excerpt"].lower()


@pytest.mark.parametrize("query", ["", "   ", '"', "AND NOT", "*", "a OR", "NEAR(", "é" * 6000, "\u0000", "drop table docs; --",
                                   "mot " * 3000, "'; DELETE FROM docs; --", "garage\" OR \"1"])
def test_search_docs_hostile_queries(mem, query):
    seed(mem)
    result = mem.search_docs(query)
    assert isinstance(result, list)
    assert len(mem.recent_docs()) == 3                                         # rien n'a été supprimé


def test_reads_do_not_touch_the_network_or_the_llm(mem, monkeypatch):
    seed(mem)
    monkeypatch.setattr(mem.router, "complete", lambda *a, **k: (_ for _ in ()).throw(AssertionError("IA appelée")))
    mem.recent_docs(); mem.search_docs("garage"); mem.get_doc("x")


def test_malformed_created_at_does_not_crash_and_excerpt_is_centered(mem):
    a, *_ = seed(mem)
    with memory._lock:
        memory._db().execute("UPDATE docs SET created_at='pas une date' WHERE id=?", (a,))
        memory._db().commit()
    assert {d["id"]: d["created_at"] for d in mem.recent_docs()}[a] is None
    assert mem.get_doc(a)["created_at"] is None
    long = mem.add_note("blabla " * 80 + "zeppelin final", "public", "telegram")
    hit = mem.search_docs("zeppelin")[0]
    assert hit["id"] == long and "zeppelin" in hit["excerpt"] and hit["excerpt"].startswith("…")
    assert len(hit["excerpt"]) <= 240
