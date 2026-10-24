"""Appels modèles (LiteLLM), retry exponentiel sur 429 et chaîne de secours."""
from __future__ import annotations

import time
from typing import Callable

MAX_RETRIES = 3
BASE_DELAY = 1.0

# branchés par router.py : journal de consommation (jetons) et des refus 429/503
token_hook: Callable[[str, int], None] | None = None
error_hook: Callable[[str, int], None] | None = None


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def is_rate_limit(exc: BaseException) -> bool:
    return (
        getattr(exc, "status_code", None) == 429
        or type(exc).__name__ == "RateLimitError"
    )


def call_model(model: str, messages: list[dict[str, str]], json_mode: bool = False,
               timeout: float | None = None) -> str:
    """Appel LiteLLM (import paresseux ; mocké dans les tests). `timeout` : secondes max pour UN appel HTTP
    (sans lui, litellm attend jusqu'à 10 min si le fournisseur ne répond plus)."""
    import litellm

    kwargs: dict[str, object] = {}
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    if timeout is not None:
        kwargs["timeout"] = timeout
    resp = litellm.completion(model=model, messages=messages, **kwargs)
    usage = getattr(resp, "usage", None)
    if token_hook and usage and getattr(usage, "total_tokens", None):
        token_hook(model, int(usage.total_tokens))
    return resp.choices[0].message.content or ""


def cloud_transcribe(model: str, audio_path: str, timeout: float | None = None) -> str:
    import litellm

    kwargs: dict[str, object] = {"timeout": timeout} if timeout is not None else {}
    with open(audio_path, "rb") as f:
        resp = litellm.transcription(model=model, file=f, **kwargs)
    return str(resp.text)


def local_transcribe(audio_path: str) -> str:
    import os

    from faster_whisper import WhisperModel

    model = WhisperModel(os.getenv("WHISPER_LOCAL_MODEL", "small"), compute_type="int8")
    segments, _ = model.transcribe(audio_path)
    return " ".join(s.text.strip() for s in segments).strip()


def call_with_retry(
    model: str,
    messages: list[dict[str, str]],
    json_mode: bool = False,
    on_attempt: Callable[[str], None] | None = None,
    timeout: float | None = None,
) -> str:
    """Retry exponentiel sur 429 uniquement ; autres erreurs (dont un délai dépassé) remontent aussitôt."""
    for attempt in range(MAX_RETRIES + 1):
        try:
            result = call_model(model, messages, json_mode, timeout)
        except Exception as exc:  # noqa: BLE001
            if error_hook and (is_rate_limit(exc) or getattr(exc, "status_code", None) == 503):
                error_hook(model, getattr(exc, "status_code", None) or 429)
            if is_rate_limit(exc) and attempt < MAX_RETRIES:
                _sleep(BASE_DELAY * 2**attempt)
                continue
            raise
        if on_attempt:
            on_attempt(model)
        return result
    raise AssertionError("unreachable")


def run_chain(
    models: list[str],
    messages: list[dict[str, str]],
    json_mode: bool = False,
    on_success: Callable[[str], None] | None = None,
    timeout: float | None = None,
) -> tuple[str, list[Exception]]:
    """Essaie chaque modèle dans l'ordre ; renvoie (réponse, erreurs) ou lève la dernière.
    `timeout` s'applique à chaque appel HTTP, pas à la chaîne entière."""
    errors: list[Exception] = []
    for model in models:
        try:
            return call_with_retry(model, messages, json_mode, on_success, timeout), errors
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
    raise ChainExhausted(errors)


class ChainExhausted(Exception):
    def __init__(self, errors: list[Exception]) -> None:
        super().__init__("; ".join(f"{type(e).__name__}: {e}" for e in errors))
        self.errors = errors
