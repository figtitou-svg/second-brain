"""Nettoyage des titres ADE (emploi du temps INSA) : `IF:3:S1::POO1:TD::3IF3 #001` -> `POO1 · TD`.

Les noms complets des matières viennent de coach/course_names.yaml (à compléter par l'utilisateur) ;
un code inconnu est affiché tel quel, jamais deviné.
"""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

import yaml

NAMES_PATH = Path(__file__).with_name("course_names.yaml")
ADE_RE = re.compile(
    r"^(?P<dept>[A-Z0-9]+):(?P<level>[A-Z0-9]+):(?P<sem>S\d+)::(?P<code>[^:]+):(?P<type>[^:]*)::(?P<group>.*?)"
    r"(?:\s*#\d+)?\s*$")
TYPES = {"CM": "CM", "TD": "TD", "TP": "TP", "TA": "TA", "EV": "Évaluation", "EDT": ""}


@lru_cache(maxsize=1)
def course_names() -> dict[str, str]:
    try:
        data = yaml.safe_load(NAMES_PATH.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        return {}
    return {str(k): str(v) for k, v in data.items() if v}


def clean_title(title: str) -> str:
    """Titre lisible pour un événement ADE ; tout autre titre est renvoyé inchangé."""
    m = ADE_RE.match(title.strip())
    if not m:
        return title
    code = m["code"]
    name = "Général" if code == "*" else course_names().get(code, code)
    kind = TYPES.get(m["type"], m["type"])
    return f"{name} · {kind}" if kind else name
