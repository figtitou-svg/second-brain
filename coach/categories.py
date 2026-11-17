"""Catégories de tâches et leurs surnoms (coach/categories.yaml) : « rajoute dans chaps … » -> « [Cha] … »."""
from __future__ import annotations

import functools
import re
import unicodedata
from pathlib import Path
from typing import Any

import yaml

from coach import focus

FILE = Path(__file__).with_name("categories.yaml")


@functools.lru_cache(maxsize=1)
def _load() -> dict[str, dict[str, Any]]:
    try:
        raw = yaml.safe_load(FILE.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}
    out = {}
    for code, v in raw.items():
        v = v if isinstance(v, dict) else {"name": str(v)}
        out[str(code)] = {"name": str(v.get("name") or code), "aliases": [str(a) for a in v.get("aliases") or []]}
    return out


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s.lower())
    return "".join(c for c in s if not unicodedata.combining(c))


def known(db: Any | None = None) -> list[dict[str, Any]]:
    """[{code, name, aliases}] : celles du fichier, puis les préfixes déjà utilisés dans les tâches."""
    cats = _load()
    out = [{"code": c, **v} for c, v in cats.items()]
    seen = {_norm(c) for c in cats}
    if db is not None:
        for t in db.list_tasks("pending", "scheduled"):
            code = focus.subject_of(t.title)
            if code and _norm(code) not in seen:
                seen.add(_norm(code))
                out.append({"code": code, "name": code, "aliases": []})
    return out


def code_of(raw: object, cats: list[dict[str, Any]]) -> str | None:
    """Le code exact (tel qu'écrit dans la liste) pour une réponse de l'IA (« cha », « [Cha] », « CHAPS »…)."""
    if not raw:
        return None
    r = _norm(str(raw).strip().strip("[]"))
    for c in cats:
        if r == _norm(c["code"]) or r == _norm(c["name"]) or r in (_norm(a) for a in c["aliases"]):
            return c["code"]
    return None


def guess(text: str, cats: list[dict[str, Any]]) -> str | None:
    """Catégorie nommée dans le texte (code ou surnom, mot entier), seulement si une seule correspond."""
    t = _norm(text)
    hits = set()
    for c in cats:   # surnoms en mots entiers ; le code seul (« pro », « per ») seulement écrit entre crochets
        if f"[{_norm(c['code'])}]" in t:
            hits.add(c["code"])
        for w in c["aliases"]:
            if re.search(r"(?<![\w])" + re.escape(_norm(w)) + r"(?![\w])", t):
                hits.add(c["code"])
    return hits.pop() if len(hits) == 1 else None


def prefixed(title: str, code: str | None) -> str:
    """« [Code] titre » si le titre n'a pas déjà de préfixe."""
    title = title.strip()
    return title if not code or focus.subject_of(title) else f"[{code}] {title}"


def prompt_block(cats: list[dict[str, Any]]) -> str:
    lines = ["Catégories de tâches de l'utilisateur (code entre crochets, puis ce qu'elle désigne) :"]
    for c in cats:
        al = f" — aussi appelée : {', '.join(c['aliases'])}" if c["aliases"] else ""
        lines.append(f"- {c['code']} : {c['name']}{al}")
    return "\n".join(lines)
