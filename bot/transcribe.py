"""Transcription vocale : délègue à llm_router.router.transcribe."""
from __future__ import annotations

from .ports import RouterPort


def transcribe_audio(audio_path: str, router: RouterPort) -> str:
    return router.transcribe(audio_path).strip()
