"""Routeur LLM : respecte `sensitivity` (private -> jamais cloud)."""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from . import fallback

Sensitivity = Literal["private", "public"]

_LOCAL_PREFIXES = ("ollama/", "ollama_chat/")


class SensitivityError(Exception):
    """Contenu private -> modèle cloud interdit (ou sensibilité invalide)."""


class PrivateModelUnavailableError(Exception):
    """Ollama indisponible : le contenu private n'est jamais envoyé au cloud."""


class LLMUnavailableError(Exception):
    """Aucun modèle de la chaîne n'a répondu."""


def _env(name: str, default: str = "") -> str:
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass
    return os.getenv(name, default)


def _data_dir() -> Path:
    d = Path(_env("LLM_ROUTER_DATA_DIR", "data"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def cloud_allowed_for_notes() -> bool:
    """Aide pour les appelants : notes perso peuvent être marquées `public` si True."""
    return _env("ALLOW_CLOUD_FOR_NOTES", "false").strip().lower() == "true"


def cloud_allowed_for_mails() -> bool:
    """Si True, les mails sont traités comme `public` (Gemini/Groq). Défaut : False (Ollama local)."""
    return _env("ALLOW_CLOUD_FOR_MAILS", "false").strip().lower() == "true"


def _is_local(model: str) -> bool:
    return model.startswith(_LOCAL_PREFIXES)


def _log_usage(model: str) -> None:
    provider = model.split("/", 1)[0]
    day = datetime.now(ZoneInfo("Europe/Paris")).date().isoformat()
    with open(_data_dir() / "usage.log", "a", encoding="utf-8") as f:
        f.write(f"{day} {provider} {model} 1\n")


def _log_line(model: str, what: str) -> None:
    day = datetime.now(ZoneInfo("Europe/Paris")).date().isoformat()
    with open(_data_dir() / "usage.log", "a", encoding="utf-8") as f:
        f.write(f"{day} {model.split('/', 1)[0]} {model} {what}\n")


fallback.token_hook = lambda model, n: _log_line(model, f"tokens={n}")
fallback.error_hook = lambda model, code: _log_line(model, f"err={code}")


DEFAULT_LLM_TIMEOUT = 60.0      # secondes par appel HTTP ; réglable avec LLM_TIMEOUT_SECONDS
DEFAULT_WHISPER_TIMEOUT = 120.0  # transcription cloud ; réglable avec WHISPER_TIMEOUT_SECONDS


def _timeout(explicit: float | None, env_name: str, default: float) -> float:
    """Délai demandé par l'appelant, sinon celui du .env, sinon la valeur par défaut (valeurs invalides ignorées)."""
    if explicit is not None:
        return float(explicit)
    try:
        value = float(_env(env_name, ""))
    except ValueError:
        return default
    return value if value > 0 else default


def complete(
    prompt: str,
    *,
    sensitivity: Sensitivity,
    system: str | None = None,
    json_mode: bool = False,
    timeout: float | None = None,
) -> str:
    """`timeout` : secondes max par appel HTTP (défaut : LLM_TIMEOUT_SECONDS, sinon 60)."""
    if sensitivity not in ("private", "public"):
        raise SensitivityError(f"sensitivity invalide: {sensitivity!r}")

    if sensitivity == "private":
        private_model = _env("LLM_PRIVATE_MODEL", "ollama/qwen2.5:7b")
        if not _is_local(private_model):
            raise SensitivityError(
                f"LLM_PRIVATE_MODEL={private_model!r} n'est pas local : refus (contenu private)."
            )
        chain = [private_model]
    else:
        chain = [
            _env("LLM_PUBLIC_MODEL", "gemini/gemini-flash-latest"),
            _env("LLM_PUBLIC_FALLBACK", "groq/llama-3.3-70b-versatile"),
            _env("LLM_PRIVATE_MODEL", "ollama/qwen2.5:7b"),
        ]

    # pas de cache ici : les prompts contiennent la date et l'heure (jamais deux fois les mêmes), et une réponse
    # figée deviendrait fausse ; l'extraction a son propre cache, indexé sur le texte seul (coach/extract.py)
    messages: list[dict[str, str]] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    try:
        result, _ = fallback.run_chain(chain, messages, json_mode, _log_usage,
                                       _timeout(timeout, "LLM_TIMEOUT_SECONDS", DEFAULT_LLM_TIMEOUT))
    except fallback.ChainExhausted as exc:
        if sensitivity == "private":
            raise PrivateModelUnavailableError(
                "Ollama indisponible ; contenu private non envoyé au cloud, "
                f"à réessayer plus tard ({exc})"
            ) from exc
        raise LLMUnavailableError(str(exc)) from exc
    return result


def transcribe(audio_path: str) -> str:
    """Transcription 100 % locale (faster-whisper) par défaut.

    Groq n'est utilisé que si WHISPER_ALLOW_CLOUD=true (les vocaux sont des notes perso).
    """
    if not Path(audio_path).is_file():
        raise FileNotFoundError(audio_path)
    if _env("WHISPER_ALLOW_CLOUD", "false").lower() == "true":
        model = _env("WHISPER_MODEL", "groq/whisper-large-v3-turbo")
        try:
            text = fallback.cloud_transcribe(
                model, audio_path, _timeout(None, "WHISPER_TIMEOUT_SECONDS", DEFAULT_WHISPER_TIMEOUT))
            _log_usage(model)
            return text
        except Exception:  # noqa: BLE001
            pass
    text = fallback.local_transcribe(audio_path)
    _log_usage("local/faster-whisper")
    return text
