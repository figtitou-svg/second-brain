"""Suivi de la consommation des API gratuites (lecture de data/usage.log) et alertes de seuil.

Lignes du journal (écrites par llm_router) :
    AAAA-MM-JJ <fournisseur> <modèle> 1            appel réussi
    AAAA-MM-JJ <fournisseur> <modèle> tokens=N     jetons consommés
    AAAA-MM-JJ <fournisseur> <modèle> err=429      limite atteinte / service saturé (503...)

Les limites gratuites changent souvent et dépendent du projet : elles sont réglables dans .env
(GEMINI_DAILY_REQUESTS, GROQ_DAILY_REQUESTS, GROQ_DAILY_TOKENS) ; à vérifier dans AI Studio / console Groq.
"""
from __future__ import annotations

import os
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Paris")
WARN_RATIO = 0.8
DEFAULT_LIMITS = {"gemini": (20, None), "groq": (1000, 200_000)}  # (requêtes/jour, jetons/jour) : estimations
LOCAL = {"ollama", "local"}


@dataclass
class Usage:
    requests: int = 0
    tokens: int = 0
    errors: int = 0


@dataclass
class Report:
    day: date
    today: dict[str, Usage] = field(default_factory=dict)
    history: dict[str, list[int]] = field(default_factory=dict)  # requêtes des 7 derniers jours (ancien -> récent)


def log_path() -> Path:
    return Path(os.getenv("LLM_ROUTER_DATA_DIR", "data")) / "usage.log"


def limits(provider: str) -> tuple[int | None, int | None]:
    env = provider.upper()
    d_req, d_tok = DEFAULT_LIMITS.get(provider, (None, None))

    def get(name: str, default: int | None) -> int | None:
        v = os.getenv(name, "").strip().strip('"')
        return int(v) if v.isdigit() else default

    return get(f"{env}_DAILY_REQUESTS", d_req), get(f"{env}_DAILY_TOKENS", d_tok)


def read(path: Path | None = None, today: date | None = None) -> Report:
    today = today or datetime.now(TZ).date()
    rep = Report(day=today)
    per_day: dict[str, dict[date, int]] = defaultdict(lambda: defaultdict(int))
    p = path or log_path()
    if not p.exists():
        return rep
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        try:
            day = date.fromisoformat(parts[0])
        except ValueError:
            continue
        provider, kind = parts[1], parts[3]
        if provider in LOCAL:
            continue
        if kind == "1":
            per_day[provider][day] += 1
            if day == today:
                rep.today.setdefault(provider, Usage()).requests += 1
        elif kind.startswith("tokens=") and day == today:
            rep.today.setdefault(provider, Usage()).tokens += int(kind.split("=", 1)[1] or 0)
        elif kind.startswith("err=") and day == today:
            rep.today.setdefault(provider, Usage()).errors += 1
    for provider, days in per_day.items():
        rep.history[provider] = [days.get(today - timedelta(days=i), 0) for i in range(6, -1, -1)]
    return rep


def _bar(ratio: float, width: int = 10) -> str:
    filled = min(width, round(ratio * width))
    return "▓" * filled + "░" * (width - filled)


def _k(n: int) -> str:
    return f"{n / 1000:.1f} k" if n >= 1000 else str(n)


def _status(ratio: float) -> str:
    return "🔴" if ratio >= 1 else "🟠" if ratio >= WARN_RATIO else "🟢"


def report(now: datetime | None = None, path: Path | None = None) -> str:
    now = (now or datetime.now(TZ)).astimezone(TZ)
    rep = read(path, now.date())
    names = {"gemini": "Gemini", "groq": "Groq"}
    providers = sorted(set(rep.today) | {"gemini", "groq"})
    lines = ["📊 <b>Consommation des API gratuites</b> (aujourd'hui)", ""]
    hours = max(now.hour + now.minute / 60, 0.5)
    for prov in providers:
        u = rep.today.get(prov, Usage())
        req_lim, tok_lim = limits(prov)
        ratio = max(u.requests / req_lim if req_lim else 0, u.tokens / tok_lim if tok_lim else 0)
        head = f"{_status(ratio)} <b>{names.get(prov, prov)}</b>"
        if req_lim:
            head += f"  {_bar(u.requests / req_lim)}  {u.requests}/{req_lim} requêtes ({u.requests / req_lim:.0%})"
        else:
            head += f"  {u.requests} requêtes"
        lines.append(head)
        if tok_lim:
            lines.append(f"      {_k(u.tokens)}/{_k(tok_lim)} jetons ({u.tokens / tok_lim:.0%})")
        elif u.tokens:
            lines.append(f"      {_k(u.tokens)} jetons")
        if u.errors:
            lines.append(f"      ⚠️ {u.errors} refus (limite ou service saturé) aujourd'hui")
        if req_lim and u.requests:
            projected = u.requests / hours * 24
            if projected > req_lim and ratio < 1:
                lines.append(f"      📈 À ce rythme, limite atteinte vers {_eta(u.requests, req_lim, hours)}")
        hist = rep.history.get(prov)
        if hist and sum(hist) > 0:
            lines.append(f"      7 derniers jours : {' · '.join(str(x) for x in hist)}")
    lines += ["", "<i>Limites estimées, réglables dans .env : vérifie les valeurs réelles dans Google AI Studio "
                  "et la console Groq.</i>"]
    return "\n".join(lines)


def _eta(used: int, limit: int, hours: float) -> str:
    hours_to_limit = limit / (used / hours)
    return f"{min(int(hours_to_limit), 23):02d}h{int((hours_to_limit % 1) * 60):02d}"


def check_thresholds(notify: Callable[[str, object], None], claim: Callable[[str], bool],
                     now: datetime | None = None, path: Path | None = None) -> None:
    """Alerte une seule fois par jour et par palier (80 % puis 100 %) et par fournisseur."""
    now = (now or datetime.now(TZ)).astimezone(TZ)
    rep = read(path, now.date())
    for prov, u in rep.today.items():
        req_lim, tok_lim = limits(prov)
        ratio = max(u.requests / req_lim if req_lim else 0, u.tokens / tok_lim if tok_lim else 0)
        for level, text in ((1.0, "limite atteinte"), (WARN_RATIO, "bientôt épuisé")):
            if ratio >= level:
                if claim(f"quota:{prov}:{now.date()}:{level}"):
                    notify(f"{'🔴' if level >= 1 else '🟠'} <b>Quota {prov} {text}</b> : {ratio:.0%} utilisé aujourd'hui."
                           + (" Le bot bascule sur le modèle de secours." if level >= 1 else "")
                           + "\nDétail : /quota", None)
                break
