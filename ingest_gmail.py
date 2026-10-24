"""Ingestion Gmail par lots (lecture seule). Mails `private` (Ollama local) sauf ALLOW_CLOUD_FOR_MAILS=true."""
from __future__ import annotations

import base64
import logging
from typing import Any, Callable

import memory
from llm_router.router import cloud_allowed_for_mails
from coach import fmt, store
from bot.handlers import event_buttons, task_buttons

log = logging.getLogger("ingest_gmail")
Notify = Callable[[str, "list[list[tuple[str, str]]] | None"], None]


def _body(payload: dict[str, Any]) -> str:
    data = payload.get("body", {}).get("data")
    if data and payload.get("mimeType", "").startswith("text/plain"):
        return base64.urlsafe_b64decode(data + "==").decode("utf-8", "replace")
    return "\n".join(_body(p) for p in payload.get("parts", []))


def poll(service: Any, notify: Notify, query: str = "in:inbox newer_than:2d") -> int:
    """Traite les mails non encore vus. Retourne le nombre de nouveaux mails."""
    ids = [m["id"] for m in service.users().messages().list(userId="me", q=query, maxResults=25)
           .execute().get("messages", [])]
    # défaut : mails privés -> Ollama local. ALLOW_CLOUD_FOR_MAILS=true -> traités par Gemini/Groq.
    sens = "public" if cloud_allowed_for_mails() else "private"
    n = 0
    for mid in ids:
        if memory.has_source(f"gmail:{mid}"):
            continue
        msg = service.users().messages().get(userId="me", id=mid, format="full").execute()
        hdr = {h["name"].lower(): h["value"] for h in msg["payload"].get("headers", [])}
        text = f"De : {hdr.get('from', '?')}\nObjet : {hdr.get('subject', '')}\n\n{_body(msg['payload'])}"
        # extraction d'abord : si le modèle ne répond pas, le mail n'est pas marqué comme vu et sera retraité
        tasks, events = store.extract_items(text, f"gmail:{mid}", sens)
        memory.add_mail(text, hdr.get("subject", "(sans objet)"), f"gmail:{mid}", sens)
        n += 1
        if len(tasks) > 1:  # un mail = en général une tâche ; s'il y en a plusieurs, on demande laquelle garder
            notify(f"📧 <b>Plusieurs tâches possibles dans ce mail</b> ({len(tasks)}) : "
                   "garde seulement celles qui comptent, ignore le reste.", None)
        for t in tasks:
            card, buttons = fmt.task_card(t, "📧 <b>Tâche dans un mail</b>"), task_buttons(t.id)
            try:
                prop = store.project_proposal(t.id)  # « 📁 Projet … ? » si l'IA a reconnu un projet
            except Exception:  # noqa: BLE001 - le mail est déjà marqué vu : la carte ne doit jamais se perdre
                log.warning("proposition de projet indisponible pour la tâche %s", t.id, exc_info=True)
                prop = None
            if prop:
                card, buttons = f"{card}\n{prop[0]}", buttons + prop[1]
            notify(card, buttons)
        for e in events:  # extrait d'un mail : on demande avant d'écrire dans l'agenda
            notify(fmt.event_card(e, "📧 <b>Rendez-vous dans un mail</b>"), event_buttons(e.id))
    return n


def build_service() -> Any:  # pragma: no cover - réseau / OAuth
    from coach.calendar_client import LazyGoogleService  # n'échoue pas au démarrage si le jeton Google est refusé
    return LazyGoogleService("gmail", "v1")
