import pytest

from llm_router import fallback, router


class RateLimit(Exception):
    status_code = 429


def fake(responses, calls):
    def call(model, messages, json_mode=False, timeout=None):
        calls.append(model)
        r = responses[model]
        if isinstance(r, list):
            r = r.pop(0)
        if isinstance(r, Exception):
            raise r
        return r
    return call


def test_public_uses_primary(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(fallback, "call_model", fake({"gemini/test": "ok"}, calls))
    assert router.complete("hi", sensitivity="public") == "ok"
    assert calls == ["gemini/test"]


def test_public_fallback_chain(monkeypatch):
    calls: list[str] = []
    resp = {"gemini/test": RateLimit(), "groq/test": RateLimit(), "ollama/test": "local"}
    monkeypatch.setattr(fallback, "call_model", fake(resp, calls))
    assert router.complete("hi", sensitivity="public") == "local"
    assert calls.count("gemini/test") == fallback.MAX_RETRIES + 1
    assert calls[-1] == "ollama/test"


def test_retry_then_success(monkeypatch):
    calls: list[str] = []
    resp = {"gemini/test": [RateLimit(), RateLimit(), "ok"]}
    monkeypatch.setattr(fallback, "call_model", fake(resp, calls))
    assert router.complete("hi", sensitivity="public") == "ok"
    assert len(calls) == 3


def test_backoff_is_exponential(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(fallback, "_sleep", sleeps.append)
    resp = {"gemini/test": [RateLimit(), RateLimit(), "ok"]}
    monkeypatch.setattr(fallback, "call_model", fake(resp, []))
    router.complete("hi", sensitivity="public")
    assert sleeps[1] == 2 * sleeps[0]


def test_private_never_cloud(monkeypatch):
    calls: list[str] = []
    resp = {"ollama/test": ConnectionError("down")}
    monkeypatch.setattr(fallback, "call_model", fake(resp, calls))
    with pytest.raises(router.PrivateModelUnavailableError):
        router.complete("secret", sensitivity="private")
    assert set(calls) == {"ollama/test"}


def test_private_ok(monkeypatch):
    monkeypatch.setattr(fallback, "call_model", fake({"ollama/test": "p"}, []))
    assert router.complete("secret", sensitivity="private") == "p"


def test_private_cloud_model_configured_is_refused(monkeypatch):
    monkeypatch.setenv("LLM_PRIVATE_MODEL", "gemini/oops")
    monkeypatch.setattr(fallback, "call_model", fake({}, []))
    with pytest.raises(router.SensitivityError):
        router.complete("secret", sensitivity="private")


def test_invalid_sensitivity():
    with pytest.raises(router.SensitivityError):
        router.complete("x", sensitivity="weird")  # type: ignore[arg-type]


def test_no_disk_cache(monkeypatch, env):
    # chaque appel est frais (une réponse à /ask ne doit pas rester figée) et rien n'est écrit sur disque
    calls: list[str] = []
    monkeypatch.setattr(fallback, "call_model", fake({"gemini/test": ["a", "b"]}, calls))
    assert router.complete("same", sensitivity="public") == "a"
    assert router.complete("same", sensitivity="public") == "b"
    assert len(calls) == 2
    assert not (env / "llm_cache").exists()


def test_usage_log(monkeypatch, env):
    monkeypatch.setattr(fallback, "call_model", fake({"gemini/test": "ok"}, []))
    router.complete("hi", sensitivity="public")
    log = (env / "usage.log").read_text()
    assert "gemini" in log and "\n" in log


def test_non_429_error_goes_to_next_without_retry(monkeypatch):
    calls: list[str] = []
    resp = {"gemini/test": RuntimeError("boom"), "groq/test": "g"}
    monkeypatch.setattr(fallback, "call_model", fake(resp, calls))
    assert router.complete("hi", sensitivity="public") == "g"
    assert calls == ["gemini/test", "groq/test"]


def test_all_fail(monkeypatch):
    resp = {m: RuntimeError("x") for m in ("gemini/test", "groq/test", "ollama/test")}
    monkeypatch.setattr(fallback, "call_model", fake(resp, []))
    with pytest.raises(router.LLMUnavailableError):
        router.complete("hi", sensitivity="public")
