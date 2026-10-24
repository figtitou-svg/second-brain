"""Un jeton Google refusé ne doit pas empêcher le bot de démarrer, ni de se rétablir seul."""
import sys
import types

import pytest

from coach import calendar_client as cc


def test_jeton_refuse_ne_leve_pas_a_la_construction_puis_reessaie(monkeypatch):
    calls = {"n": 0}

    def fake_creds(token_file):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("invalid_grant")
        return "creds"

    built = []
    discovery = types.ModuleType("googleapiclient.discovery")
    discovery.build = lambda api, version, credentials, cache_discovery: built.append((api, credentials)) or types.SimpleNamespace(ping=lambda: "ok")
    monkeypatch.setitem(sys.modules, "googleapiclient", types.ModuleType("googleapiclient"))
    monkeypatch.setitem(sys.modules, "googleapiclient.discovery", discovery)
    monkeypatch.setattr(cc, "credentials_from_env", fake_creds)

    svc = cc.LazyGoogleService("calendar", "v3")  # ne touche pas à Google
    assert calls["n"] == 0
    with pytest.raises(RuntimeError):
        svc.ping
    assert svc.ping() == "ok"  # jeton remplacé : reprise sans redémarrage
    assert built == [("calendar", "creds")]
