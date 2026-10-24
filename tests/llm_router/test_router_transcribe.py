import pytest

from llm_router import fallback, router


def test_local_by_default(monkeypatch, tmp_path):
    f = tmp_path / "a.ogg"
    f.write_bytes(b"x")
    monkeypatch.setenv("WHISPER_ALLOW_CLOUD", "false")  # load_dotenv() ne doit pas relire le .env réel
    monkeypatch.setattr(fallback, "cloud_transcribe", lambda m, p, timeout=None: pytest.fail("cloud interdit"))
    monkeypatch.setattr(fallback, "local_transcribe", lambda p: "local")
    assert router.transcribe(str(f)) == "local"


def test_groq_first(monkeypatch, tmp_path):
    monkeypatch.setenv("WHISPER_ALLOW_CLOUD", "true")
    f = tmp_path / "a.ogg"
    f.write_bytes(b"x")
    monkeypatch.setattr(fallback, "cloud_transcribe", lambda m, p, timeout=None: "bonjour")
    monkeypatch.setattr(fallback, "local_transcribe", lambda p: "local")
    assert router.transcribe(str(f)) == "bonjour"


def test_local_fallback(monkeypatch, tmp_path):
    monkeypatch.setenv("WHISPER_ALLOW_CLOUD", "true")
    f = tmp_path / "a.ogg"
    f.write_bytes(b"x")

    def boom(m, p, timeout=None):
        raise RuntimeError("down")

    monkeypatch.setattr(fallback, "cloud_transcribe", boom)
    monkeypatch.setattr(fallback, "local_transcribe", lambda p: "local")
    assert router.transcribe(str(f)) == "local"


def test_missing_file():
    with pytest.raises(FileNotFoundError):
        router.transcribe("/nonexistent.ogg")
