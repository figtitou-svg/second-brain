import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


@pytest.fixture(autouse=True)
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_PUBLIC_MODEL", "gemini/test")
    monkeypatch.setenv("LLM_PUBLIC_FALLBACK", "groq/test")
    monkeypatch.setenv("LLM_PRIVATE_MODEL", "ollama/test")
    monkeypatch.setenv("WHISPER_MODEL", "groq/whisper-test")
    monkeypatch.setenv("ALLOW_CLOUD_FOR_NOTES", "false")
    monkeypatch.setenv("LLM_ROUTER_DATA_DIR", str(tmp_path))
    from llm_router import fallback
    monkeypatch.setattr(fallback, "_sleep", lambda s: None)
    return tmp_path
