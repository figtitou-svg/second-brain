# web/actions.py
"""Pont entre la page web et le BotService partagé avec Telegram : saisie, commandes, clics, Discussion."""
from __future__ import annotations

import html
import logging
import re
import threading
import time
from collections import deque
from dataclasses import dataclass

from bot.handlers import BotService, Reply

log = logging.getLogger("web.actions")

CLICK_FAILED = "⚠️ Ça n'a pas marché, rien n'a été enregistré. Réessaie."
_ALLOWED = re.compile(r'^</?(b|i|u|s|code|pre|blockquote)>$|^<a href="https?://[^"<>]+">$|^</a>$')
_TAG = re.compile(r"<[^>]*>")


def safe_html(text: str) -> str:
    """HTML de Telegram (déjà échappé par le bot) : seules ses balises passent, le reste est échappé.
    Équilibré : une fermante orpheline disparaît, les balises restées ouvertes sont refermées à la fin (sinon le
    gras d'une bulle déborderait sur les suivantes)."""
    out, pos, opened = [], 0, []
    for m in _TAG.finditer(text):
        out.append(text[pos:m.start()].replace("<", "&lt;").replace(">", "&gt;"))
        tag = m.group(0)
        pos = m.end()
        if not _ALLOWED.match(tag):
            out.append(html.escape(tag, quote=True))
            continue
        name = re.match(r"</?(\w+)", tag).group(1)
        if not tag.startswith("</"):
            opened.append(name)
            out.append(tag)
        elif name in opened:
            while opened:                          # referme aussi ce qui a été ouvert dedans
                inner = opened.pop()
                out.append(f"</{inner}>")
                if inner == name:
                    break
    out.append(text[pos:].replace("<", "&lt;").replace(">", "&gt;"))
    out.extend(f"</{name}>" for name in reversed(opened))
    return "".join(out).replace("\n", "<br>")


# commande -> méthode de BotService, exactement comme les gestionnaires Telegram
_NO_ARG = {"today": "today", "tasks": "tasks", "replan": "replan", "focus": "focus", "projet": "projet",
           "projets": "projets", "annuler": "annuler", "semaine": "week", "quota": "quota", "help": "help",
           "start": "help"}


def submit(svc: BotService, text: str) -> list[Reply]:
    """Barre de saisie et Discussion : /commande ou message libre (note, question, tâche…)."""
    text = text.strip()
    if not text:
        return []
    if not text.startswith("/"):
        return svc.on_text(text, source="web")
    head, _, rest = text[1:].partition(" ")
    name, rest = head.split("@")[0].lower(), rest.strip()
    if name in _NO_ARG:
        return getattr(svc, _NO_ARG[name])()
    if name == "ask":
        return svc.ask(rest)
    if name == "note":
        return svc.note(rest)
    if name == "done":
        return svc.done(rest.split()[0] if rest else "")
    return [Reply(f"❓ Commande inconnue : /{html.escape(name)}. Tape /help pour la liste.")]


def click(svc: BotService, data: str) -> Reply:
    """Bouton de la page : même chaîne de rappel que le bouton Telegram."""
    try:
        return svc.on_callback(data)
    except Exception:  # noqa: BLE001 - jamais de trace Python à l'écran
        log.exception("clic web %r échoué", data)
        return Reply(CLICK_FAILED)


@dataclass(frozen=True)
class Exchange:
    at: float
    said: str
    replies: list[Reply]
    ref: int | None = None


class Discussion:
    """Historique de la Discussion web, en mémoire (perdu au redémarrage du bot)."""

    def __init__(self, limit: int = 50) -> None:
        self._items: deque[Exchange] = deque(maxlen=limit)
        self._mutex = threading.Lock()

    def add(self, said: str, replies: list[Reply], ref: int | None = None) -> Exchange:
        ex = Exchange(time.time(), said, list(replies), ref)
        with self._mutex:
            self._items.append(ex)
        return ex

    def find(self, ref: int) -> Exchange | None:
        with self._mutex:
            return next((e for e in self._items if e.ref == ref), None)

    def items(self) -> list[Exchange]:
        with self._mutex:
            return list(self._items)
