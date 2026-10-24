import sys
from types import SimpleNamespace

from llm_router import fallback, router


def fake_litellm(seen):
    """Faux module litellm : garde les arguments reçus, sans réseau."""
    def completion(**kw):
        seen.append(kw)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))], usage=None)

    def transcription(**kw):
        seen.append(kw)
        return SimpleNamespace(text="bonjour")

    return SimpleNamespace(completion=completion, transcription=transcription)


def timeouts_seen(monkeypatch):
    seen = []

    def call(model, messages, json_mode=False, timeout=None):
        seen.append(timeout)
        return "ok"

    monkeypatch.setattr(fallback, "call_model", call)
    return seen


def test_call_model_passes_the_timeout_to_litellm(monkeypatch):
    seen = []
    monkeypatch.setitem(sys.modules, "litellm", fake_litellm(seen))
    assert fallback.call_model("groq/x", [{"role": "user", "content": "hi"}], False, 7.5) == "ok"
    assert seen[0]["timeout"] == 7.5


def test_call_model_without_timeout_sends_none(monkeypatch):
    seen = []
    monkeypatch.setitem(sys.modules, "litellm", fake_litellm(seen))
    fallback.call_model("groq/x", [{"role": "user", "content": "hi"}])
    assert "timeout" not in seen[0]


def test_cloud_transcribe_passes_the_timeout(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setitem(sys.modules, "litellm", fake_litellm(seen))
    f = tmp_path / "a.ogg"
    f.write_bytes(b"x")
    assert fallback.cloud_transcribe("groq/whisper", str(f), 30.0) == "bonjour"
    assert seen[0]["timeout"] == 30.0


def test_complete_default_timeout_is_60_seconds(monkeypatch):
    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", "")
    seen = timeouts_seen(monkeypatch)
    router.complete("x", sensitivity="public")
    assert seen == [60.0]


def test_complete_timeout_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", "7")
    seen = timeouts_seen(monkeypatch)
    router.complete("x", sensitivity="public")
    assert seen == [7.0]


def test_complete_ignores_an_invalid_environment_value(monkeypatch):
    seen = timeouts_seen(monkeypatch)
    for bad in ("abc", "0", "-5"):
        monkeypatch.setenv("LLM_TIMEOUT_SECONDS", bad)
        router.complete("x", sensitivity="public")
    assert seen == [60.0, 60.0, 60.0]


def test_complete_explicit_timeout_wins_over_the_environment(monkeypatch):
    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", "7")
    seen = timeouts_seen(monkeypatch)
    router.complete("x", sensitivity="public", timeout=3)
    assert seen == [3.0]


def test_every_model_of_the_chain_gets_the_timeout(monkeypatch):
    class RateLimit(Exception):
        status_code = 429

    seen = []

    def call(model, messages, json_mode=False, timeout=None):
        seen.append((model, timeout))
        if model != "ollama/test":
            raise RateLimit()
        return "local"

    monkeypatch.setattr(fallback, "call_model", call)
    assert router.complete("x", sensitivity="public", timeout=5) == "local"
    assert {t for _, t in seen} == {5.0}
    assert seen[-1][0] == "ollama/test"


def test_transcribe_cloud_timeout_default_and_env(monkeypatch, tmp_path):
    f = tmp_path / "a.ogg"
    f.write_bytes(b"x")
    monkeypatch.setenv("WHISPER_ALLOW_CLOUD", "true")
    seen = []
    monkeypatch.setattr(fallback, "cloud_transcribe", lambda m, p, timeout=None: seen.append(timeout) or "ok")
    monkeypatch.setenv("WHISPER_TIMEOUT_SECONDS", "")
    router.transcribe(str(f))
    monkeypatch.setenv("WHISPER_TIMEOUT_SECONDS", "30")
    router.transcribe(str(f))
    assert seen == [120.0, 30.0]
