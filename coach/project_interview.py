"""Entretien de définition d'un projet (`/projet`) : machine à états enregistrée en base (survit aux redémarrages).

Étapes (`project_draft.stage`) : awaiting_description -> asking (une question à la fois) -> reviewing ->
awaiting_correction -> reviewing. Les réponses sont rangées dans la fiche sans appel IA ; seuls l'extraction de la
description et les corrections libres appellent l'IA. Sans IA : repli sur 4 questions fixes.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import date, datetime, timedelta
from typing import Any, Callable
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, ValidationError, field_validator

from bot.dates import fold, parse_date
from coach import fmt, project_service, projects
from coach.db import Database
from coach.focus import subject_of
from coach.models import Milestone, Project

log = logging.getLogger("coach.project_interview")
TZ = ZoneInfo("Europe/Paris")
Buttons = list[list[tuple[str, str]]]
Msg = tuple[str, Buttons]
LLMFn = Callable[..., str]

MAX_QUESTIONS = 5
EXPIRY = timedelta(hours=24)
ORDER = ("name", "goal", "deadline", "limits", "category", "deliverables", "weekly_hours", "team")
FIXED_ORDER = ("name", "goal", "deadline", "category")
PROMPTS = {
    "name": "Comment s'appelle le projet ?",
    "goal": "Quel est l'objectif du projet, en une ou deux phrases ?",
    "deadline": "Pour quand est-il à rendre ? (une date, ou « sans échéance »)",
    "limits": "Quelles sont les limites du projet : ce qui est inclus, et ce qui ne l'est pas ?",
    "category": "Quelle catégorie (matière) regroupe ses tâches ?",
    "deliverables": "Quels sont les livrables attendus ?",
    "weekly_hours": "Combien de temps par semaine peux-tu lui consacrer ?",
    "team": "Travailles-tu seul ou en équipe ? Avec qui ?",
}
TEXT_KEYS = ("goal", "limits", "deliverables", "weekly_hours", "team")
MAX_CATEGORY_CHARS = 40
NO_DEADLINE = ("sans echeance", "pas d'echeance", "pas de date", "aucune", "non", "rien")
NO_CATEGORY = ("aucune", "aucun", "pas de categorie")
BUSY = ("⏳ Un entretien est déjà en cours. Réponds-y, ou /annuler pour l'abandonner.", [[("Annuler", "pi:cancel")]])

EXTRACT_SYSTEM = (
    "Tu aides un étudiant à définir un projet. À partir de sa description, extrais UNIQUEMENT ce qui est dit "
    "(n'invente rien) et réponds avec un JSON : "
    '{"name":str|null,"category":str|null,"goal":str|null,"deadline":"AAAA-MM-JJ"|null,"limits":str|null,'
    '"deliverables":str|null,"team":str|null,"weekly_hours":str|null,'
    '"milestones":[{"title":str,"due":"AAAA-MM-JJ"|null}]}. '
    "category = la matière ou catégorie citée (ex. IA, Génie logiciel, Stage), sinon null. "
    "milestones = 3 à 6 étapes logiques et ordonnées du projet, déduites de la description. "
    "La date actuelle est fournie : utilise-la pour les dates relatives. Aucun texte hors JSON.")
CORRECT_SYSTEM = (
    "Tu mets à jour la fiche d'un projet. On te donne la fiche actuelle (JSON) et une demande de correction en "
    "français. Renvoie la fiche COMPLÈTE mise à jour, avec exactement le même format JSON que la fiche actuelle. "
    "Ne change que ce que la demande vise. Aucun texte hors JSON.")


class SheetMilestone(BaseModel):
    id: str | None = None   # id d'une étape existante (correction d'un projet) ; absent = étape nouvelle
    title: str = Field(min_length=1)
    due: date | None = None

    @field_validator("id", mode="before")
    @classmethod
    def _id(cls, v: Any) -> str | None:
        return (v.strip() or None) if isinstance(v, str) else None


class Sheet(BaseModel):
    """Fiche de projet renvoyée par l'IA (tout est facultatif : on ne pose que les questions qui manquent)."""
    name: str | None = None
    category: str | None = None
    goal: str | None = None
    deadline: date | None = None
    limits: str | None = None
    deliverables: str | None = None
    team: str | None = None
    weekly_hours: str | None = None
    milestones: list[SheetMilestone] = Field(default_factory=list)

    @field_validator("deadline", mode="before")
    @classmethod
    def _no_deadline(cls, v: Any) -> Any:
        return None if isinstance(v, str) and not v.strip() else v  # "" = sans échéance

    @field_validator("name", "category", "goal", "limits", "deliverables", "team", "weekly_hours", mode="before")
    @classmethod
    def _text(cls, v: Any) -> str | None:
        return None if v is None else (str(v).strip() or None)


def _json_object(raw: str) -> Any:
    a, b = raw.find("{"), raw.rfind("}")
    if a == -1 or b <= a:
        raise ValueError("pas de JSON dans la réponse")
    return json.loads(raw[a: b + 1])


class Interview:
    def __init__(self, db: Database, llm: LLMFn | None, sensitivity: str = "private",
                 subjects: dict[str, str] | None = None, now_fn: Callable[[], datetime] | None = None) -> None:
        self.db, self.llm, self.sensitivity = db, llm, sensitivity
        self.subjects = subjects or {}
        self._now = now_fn or (lambda: datetime.now(TZ))
        self._lock = threading.RLock()  # les boutons sont traités en parallèle : un double clic ne crée qu'un projet

    # --- état -------------------------------------------------------------------------
    def _save(self, stage: str, profile: dict, asked: int, pending: str | None) -> None:
        self.db.save_draft(stage, profile, asked, pending, self._now())

    def active(self) -> bool:
        d = self.db.get_draft()
        if d is None:
            return False
        if self._now() - d["updated_at"] > EXPIRY:  # un entretien oublié ne doit pas avaler les notes
            self.db.clear_draft()
            return False
        return True

    def start(self) -> list[Msg]:
        with self._lock:
            if self.active():
                return [BUSY]
            self._save("awaiting_description", {}, 0, None)
            return [("🆕 <b>Nouveau projet</b>\nDécris-le comme tu veux, par message ou vocal : objectif, date de "
                     "rendu, catégorie, équipe, étapes… Je te poserai ensuite les questions qui manquent.",
                     [[("Annuler", "pi:cancel")]])]

    def cancel(self) -> list[Msg]:
        with self._lock:
            had = self.active()
            self.db.clear_draft()
            return [("🗑 Entretien annulé." if had else "Aucun entretien en cours.", [])]

    # --- entrées ----------------------------------------------------------------------
    def on_text(self, text: str) -> list[Msg]:
        with self._lock:
            return self._on_text(text)

    def on_callback(self, rest: str) -> list[Msg]:
        with self._lock:
            return self._on_callback(rest)

    def _on_text(self, text: str) -> list[Msg]:
        if not self.active():  # aucun entretien, ou expiré (brouillon effacé)
            return []
        d = self.db.get_draft()
        text = text.strip()
        stage, profile, asked = d["stage"], d["profile"], d["questions_asked"]
        if stage == "awaiting_description":
            return self._extract(text)
        if stage == "asking":
            return self._answer(profile, asked, d["pending"], text)
        if stage == "awaiting_correction":
            return self._correct(profile, asked, text)
        if stage == "reviewing":
            return [("Utilise les boutons ci-dessous pour créer le projet, le corriger ou l'annuler.", []),
                    self._review(profile)]
        if stage == "add_milestone":
            return self._add_milestone(profile, text)
        if stage == "fix_project":
            return self._fix_project(profile, text)
        return []

    def _on_callback(self, rest: str) -> list[Msg]:
        if rest == "cancel":
            return self.cancel()
        if not self.active():  # aucun entretien, ou expiré (brouillon effacé)
            return [("👌 Cet entretien est terminé, rien n'a changé.", [])]
        d = self.db.get_draft()
        stage, profile, asked, key = d["stage"], d["profile"], d["questions_asked"], d["pending"]
        if stage == "asking":
            if rest == "skip":
                profile.setdefault("skipped", []).append(key)
                profile.pop("dl_retry", None)
                return self._advance(profile, asked + 1)
            if rest == "nodl" and key == "deadline":
                profile["deadline"] = ""
                profile.pop("dl_retry", None)
                return self._advance(profile, asked + 1)
            if rest == "nocat" and key == "category":
                profile.setdefault("skipped", []).append("category")
                return self._advance(profile, asked + 1)
            if rest.startswith("cat:") and key == "category":
                profile["category"] = projects.resolve_category(rest[4:], self.subjects)
                return self._advance(profile, asked + 1)
        if stage in ("reviewing", "awaiting_correction"):  # après « Corriger », Créer/Corriger restent actifs
            if rest == "ok":
                return self._create(profile)
            if rest == "fix":
                self._save("awaiting_correction", profile, asked, None)
                return [("✏️ Dis-moi ce qu'il faut changer (objectif, date, étapes à ajouter, renommer ou retirer…).",
                         [[("Annuler", "pi:cancel")]])]
        return [("👌 Ce bouton ne correspond plus à l'entretien en cours.", [])]

    # --- IA ---------------------------------------------------------------------------
    def _ask_sheet(self, system: str, prompt: str) -> Sheet | None:
        """Un essai + un seul nouvel essai ; None si l'IA est indisponible ou renvoie deux fois du JSON invalide."""
        if self.llm is None:
            return None
        err: Exception | None = None
        for attempt in range(2):
            p = prompt if attempt == 0 else (
                f"{prompt}\n\nTa réponse précédente était invalide ({err}). Réponds uniquement avec le JSON demandé.")
            try:
                raw = self.llm(p, sensitivity=self.sensitivity, system=system, json_mode=True)
            except Exception:  # noqa: BLE001 - quota, réseau, refus du routeur : on bascule sur les questions fixes
                log.warning("entretien : IA indisponible", exc_info=True)
                return None
            try:
                return Sheet.model_validate(_json_object(raw))
            except (ValueError, ValidationError) as e:
                err = e
        log.warning("entretien : fiche invalide deux fois (%s)", err)
        return None

    def _merge(self, profile: dict, sheet: Sheet) -> None:
        """Première extraction : la fiche remplace tout ; une date absente reste inconnue (on la demandera)."""
        profile["name"] = sheet.name
        profile["category"] = projects.resolve_category(sheet.category, self.subjects) if sheet.category else None
        for key in TEXT_KEYS:
            profile[key] = getattr(sheet, key)
        profile["deadline"] = sheet.deadline.isoformat() if sheet.deadline else None
        profile["milestones"] = self._sheet_milestones(sheet)

    @staticmethod
    def _sheet_milestones(sheet: Sheet) -> list[dict]:
        return [{"title": m.title, "due": m.due.isoformat() if m.due else None} for m in sheet.milestones]

    def _merge_given(self, profile: dict, sheet: Sheet) -> None:
        """Correction du brouillon : seuls les champs réellement renvoyés par l'IA s'appliquent (null explicite =
        effacé ; pour la date, « sans échéance »). Un champ absent garde sa valeur."""
        given = sheet.model_fields_set
        if "name" in given:
            profile["name"] = sheet.name
        if "category" in given:
            profile["category"] = projects.resolve_category(sheet.category, self.subjects) if sheet.category else None
        for key in TEXT_KEYS:
            if key in given:
                profile[key] = getattr(sheet, key)
        if "deadline" in given:
            profile["deadline"] = sheet.deadline.isoformat() if sheet.deadline else ""
        if "milestones" in given:
            profile["milestones"] = self._sheet_milestones(sheet)

    def _extract(self, text: str) -> list[Msg]:
        today = self._now().date().isoformat()
        sheet = self._ask_sheet(EXTRACT_SYSTEM, f"Date actuelle : {today}\n\nDescription :\n{text}")
        profile: dict[str, Any] = {"skipped": [], "milestones": [], "fallback": sheet is None}
        if sheet is None:
            profile["goal"] = text[:500]  # la description sert d'objectif ; on posera les 4 questions fixes
        else:
            self._merge(profile, sheet)
        return self._advance(profile, 0)

    def _correct(self, profile: dict, asked: int, text: str) -> list[Msg]:
        sheet_now = {k: profile.get(k) for k in ("name", "category", "goal", "deadline", "limits",
                                                  "deliverables", "team", "weekly_hours", "milestones")}
        sheet_now["deadline"] = sheet_now["deadline"] or None  # "" (sans échéance) -> null pour l'IA
        current = json.dumps(sheet_now, ensure_ascii=False)
        sheet = self._ask_sheet(CORRECT_SYSTEM, f"Date actuelle : {self._now().date().isoformat()}\n\n"
                                                f"Fiche actuelle :\n{current}\n\nDemande de correction :\n{text}")
        self._save("reviewing", profile, asked, None)
        if sheet is None:
            return [("🤔 Je n'ai pas réussi à appliquer cette correction.", []), self._review(profile)]
        self._merge_given(profile, sheet)
        self._save("reviewing", profile, asked, None)
        return [self._review(profile)]

    # --- questions --------------------------------------------------------------------
    def _next_key(self, profile: dict, asked: int) -> str | None:
        if asked >= MAX_QUESTIONS:
            return None
        skipped = profile.get("skipped", [])
        for key in (FIXED_ORDER if profile.get("fallback") else ORDER):
            if key in skipped:
                continue
            if key == "deadline":
                missing = profile.get("deadline") is None
            elif key == "category":
                missing = profile.get("category") is None
            else:
                missing = not profile.get(key)
            if missing:
                return key
        return None

    def _advance(self, profile: dict, asked: int, force: str | None = None) -> list[Msg]:
        key = force or self._next_key(profile, asked)
        if key is None:
            self._save("reviewing", profile, asked, None)
            return [self._review(profile)]
        self._save("asking", profile, asked, key)
        return [self._question(key)]

    def _category_choices(self) -> list[str]:
        taken = {projects.fold(p.category) for p in self.db.list_projects("active", "paused") if p.category}
        codes = set(self.subjects.values()) | {c for t in self.db.list_tasks("pending", "scheduled")
                                               if (c := subject_of(t.title))}
        return sorted((c for c in codes if len(c.encode()) <= 40 and projects.fold(c) not in taken), key=str.lower)[:9]

    def _question(self, key: str) -> Msg:
        text = PROMPTS[key]
        if key == "name":
            return text, [[("Annuler", "pi:cancel")]]
        if key == "deadline":
            return text, [[("Sans échéance", "pi:nodl"), ("Passer", "pi:skip")]]
        if key == "category":
            codes = self._category_choices()
            rows = [[(c, f"pi:cat:{c}") for c in codes[i:i + 3]] for i in range(0, len(codes), 3)]
            rows.append([("Aucune", "pi:nocat")])
            return text + "\n<i>Choisis, ou écris-la.</i>", rows
        return text, [[("Passer", "pi:skip")]]

    def _answer(self, profile: dict, asked: int, key: str | None, text: str) -> list[Msg]:
        word = fold(text.strip())
        if key == "category" and word in NO_CATEGORY:   # comme le bouton « Aucune »
            profile.setdefault("skipped", []).append("category")
            return self._advance(profile, asked + 1)
        if key and key != "name" and word == "passer":          # comme le bouton « Passer » (le nom n'en a pas)
            profile.setdefault("skipped", []).append(key)
            profile.pop("dl_retry", None)
            return self._advance(profile, asked + 1)
        if key == "deadline":
            if fold(text) in NO_DEADLINE:
                profile["deadline"] = ""
            else:
                d = parse_date(text, self._now().date())
                if d is not None:
                    profile["deadline"] = d.isoformat()
                elif profile.get("dl_retry"):
                    profile.setdefault("skipped", []).append("deadline")
                else:
                    profile["dl_retry"] = True
                    self._save("asking", profile, asked, "deadline")
                    return [("🤔 Je n'ai pas compris cette date. Essaie « 15 décembre », « 12/01 » ou « sans échéance ».",
                             [[("Sans échéance", "pi:nodl"), ("Passer", "pi:skip")]])]
            profile.pop("dl_retry", None)  # date réglée (lue, sans échéance ou abandonnée)
        elif key == "category":
            cat = projects.resolve_category(text, self.subjects)
            if cat == text.strip() and (len(cat) > MAX_CATEGORY_CHARS or len(cat.split()) > 4):
                # inconnue de Notion et trop longue : un morceau de description, pas un nom
                self._save("asking", profile, asked, "category")
                return [("🤔 Ça ressemble à une description, pas à une catégorie. Donne-moi un nom court "
                         "(ex. « Job », « IA »), choisis un bouton, ou « Aucune ».", self._question("category")[1])]
            profile["category"] = cat
            if profile["category"] is None:
                profile.setdefault("skipped", []).append("category")
        elif key == "name":
            profile["name"] = text[:80]
        elif key in TEXT_KEYS:
            profile[key] = text[:500]
        return self._advance(profile, asked + 1)

    # --- ajouter une étape, corriger un projet existant -------------------------------------
    def begin_add_milestone(self, project_id: str) -> list[Msg]:
        with self._lock:
            if self.active():
                return [BUSY]
            p = self.db.get_project(project_id)
            if p is None:
                return [("❓ Projet introuvable.", [])]
            self._save("add_milestone", {"project_id": project_id}, 0, None)
            return [(f"➕ Quel est le titre de la nouvelle étape de <b>{fmt.esc(p.name)}</b> ?",
                     [[("Annuler", "pi:cancel")]])]

    def _add_milestone(self, profile: dict, text: str) -> list[Msg]:
        pid, title = profile["project_id"], text[:80]
        gone = self._gone(pid)
        if gone is not None:
            return [gone]
        if not title:
            return [("Écris le titre de l'étape.", [[("Annuler", "pi:cancel")]])]
        existing = self.db.list_milestones(pid)
        position = max((m.position for m in existing), default=-1) + 1
        self.db.save_milestone(Milestone(project_id=pid, title=title, position=position))
        self.db.clear_draft()
        return [(f"✅ Étape ajoutée : <i>{fmt.esc(title)}</i>", []),
                project_service.detail(self.db, pid, self._now())]

    def _gone(self, project_id: str) -> Msg | None:
        """Le projet visé par l'entretien a disparu ou a été terminé entre-temps : on referme sans rien écrire."""
        p = self.db.get_project(project_id)
        if p is not None and p.status != "done":
            return None
        self.db.clear_draft()
        return ("❓ Projet introuvable." if p is None else "👌 Ce projet est terminé, rien n'a changé."), []

    def begin_fix_project(self, project_id: str) -> list[Msg]:
        with self._lock:
            if self.active():
                return [BUSY]
            p = self.db.get_project(project_id)
            if p is None:
                return [("❓ Projet introuvable.", [])]
            self._save("fix_project", {"project_id": project_id}, 0, None)
            return [("✏️ Dis-moi ce qu'il faut changer : le nom, l'objectif, la date, les limites, ou les étapes "
                     "(renommer, retirer, réordonner, ajouter).", [[("Annuler", "pi:cancel")]])]

    def _fix_project(self, profile: dict, text: str) -> list[Msg]:
        pid = profile["project_id"]
        gone = self._gone(pid)
        if gone is not None:
            return [gone]
        p = self.db.get_project(pid)
        assert p is not None
        ms = self.db.list_milestones(pid)
        current = json.dumps({
            "name": p.name, "goal": p.profile.get("goal"), "deadline": p.deadline.isoformat() if p.deadline else None,
            "limits": p.profile.get("limits"), "deliverables": p.profile.get("deliverables"),
            "team": p.profile.get("team"), "weekly_hours": p.profile.get("weekly_hours"),
            "milestones": [{"id": m.id, "title": m.title, "due": m.due.isoformat() if m.due else None} for m in ms]},
            ensure_ascii=False)
        sheet = self._ask_sheet(CORRECT_SYSTEM + " Garde tels quels les titres d'étapes que la demande ne vise pas."
                                " Chaque étape de la fiche actuelle a un champ id : garde l'id de toute étape "
                                "conservée OU RENOMMÉE ; une étape nouvelle n'a pas d'id.",
                                f"Date actuelle : {self._now().date().isoformat()}\n\nFiche actuelle :\n{current}"
                                f"\n\nDemande de correction :\n{text}")
        if sheet is None:  # rien ne change ; l'utilisateur peut reformuler ou annuler
            return [("🤔 Je n'ai pas réussi à appliquer cette correction. Reformule, ou annule.",
                     [[("Annuler", "pi:cancel")]])]
        given = sheet.model_fields_set  # seuls les champs réellement renvoyés par l'IA s'appliquent
        if "milestones" in given and not sheet.milestones and ms:   # liste vide : réponse jugée invalide
            return [("🤔 Je n'ai pas réussi à appliquer cette correction. Reformule, ou annule.",
                     [[("Annuler", "pi:cancel")]])]
        # 1) tout calculer, 2) écrire : une réponse rejetée ne touche pas à la base
        new_profile = dict(p.profile)
        for k in TEXT_KEYS:
            if k in given:
                value = getattr(sheet, k)
                if value:
                    new_profile[k] = value
                else:
                    new_profile.pop(k, None)
        updates: dict[str, Any] = {"profile": new_profile}
        if "name" in given and sheet.name:
            updates["name"] = sheet.name[:80]
        if "deadline" in given:
            updates["deadline"] = sheet.deadline  # null explicite = sans échéance
        saves: list[Milestone] = []
        deletes: list[str] = []
        if "milestones" in given:
            by_id = {m.id: m for m in ms}
            by_title: dict[str, Milestone] = {}
            for m in ms:
                by_title.setdefault(projects.fold(m.title), m)   # doublons : la première étape l'emporte
            kept: set[str] = set()
            for i, sm in enumerate(sheet.milestones):
                old = by_id.get(sm.id) if sm.id and sm.id not in kept else None   # d'abord par id (renommage)
                renamed = old is not None
                if old is None:
                    old = by_title.get(projects.fold(sm.title))
                if old is not None and old.id not in kept:  # étape conservée : même id, statut préservé
                    kept.add(old.id)
                    changes: dict[str, Any] = {"position": i, "due": sm.due or old.due}
                    if renamed:
                        changes["title"] = sm.title[:80]
                    saves.append(old.model_copy(update=changes))
                else:
                    saves.append(Milestone(project_id=pid, title=sm.title[:80], position=i, due=sm.due))
            for m in ms:
                if m.id in kept:
                    continue
                if m.status == "done":  # une étape franchie n'est jamais supprimée par une correction
                    saves.append(m.model_copy(update={"position": len(saves)}))
                else:
                    deletes.append(m.id)
        self.db.save_project(p.model_copy(update=updates))
        for m in saves:
            self.db.save_milestone(m)
        for mid in deletes:
            self.db.delete_milestone(mid)
        self.db.clear_draft()
        return [("✅ Projet mis à jour.", []), project_service.detail(self.db, pid, self._now())]

    # --- fiche et création ------------------------------------------------------------
    def _review(self, profile: dict) -> Msg:
        cat = profile.get("category")
        head = f"<b>{fmt.esc(profile.get('name') or '(sans nom)')}</b>"
        head += f" · catégorie <code>{fmt.esc(cat)}</code>" if cat else " · sans catégorie"
        lines = ["📋 <b>Fiche du projet</b>", head]
        if profile.get("goal"):
            lines.append(f"🎯 {fmt.esc(profile['goal'])}")
        dl = profile.get("deadline")
        lines.append("📆 " + (fmt.day_long(date.fromisoformat(dl)) if dl else "sans échéance"))
        for key, label in (("limits", "🚧 Limites"), ("deliverables", "📦 Livrables"), ("team", "👥 Équipe"),
                           ("weekly_hours", "⏱ Temps par semaine")):
            if profile.get(key):
                lines.append(f"{label} : {fmt.esc(profile[key])}")
        ms = profile.get("milestones") or []
        if ms:
            lines.append("")
            lines.append("<b>Étapes</b>")
            lines += [f"{i}. {fmt.esc(m['title'])}" + (f" (avant {fmt.day_short(date.fromisoformat(m['due']))})"
                                                       if m.get("due") else "") for i, m in enumerate(ms, 1)]
        return "\n".join(lines), [[("✅ Créer", "pi:ok"), ("✏️ Corriger", "pi:fix")], [("Annuler", "pi:cancel")]]

    def _create(self, profile: dict) -> list[Msg]:
        asked = self.db.get_draft()["questions_asked"]
        if not profile.get("name"):
            profile["skipped"] = [k for k in profile.get("skipped", []) if k != "name"]
            return self._advance(profile, asked, force="name")
        cat = profile.get("category")
        owner = projects.category_owner(self.db.list_projects(), cat) if cat else None
        if owner is not None:  # catégorie prise entre-temps : on la redemande (hors des 5 questions)
            return self._category_taken(profile, asked, cat, owner)
        now = self._now()
        dl = profile.get("deadline")
        project = Project(name=profile["name"], category=cat, deadline=date.fromisoformat(dl) if dl else None,
                          profile={k: profile[k] for k in TEXT_KEYS if profile.get(k)}, last_activity=now)
        try:
            self.db.save_project(project)
        except sqlite3.IntegrityError:  # index unique sur la catégorie : prise à l'instant par un autre projet
            log.warning("entretien : catégorie %r prise pendant la création", cat)
            owner = projects.category_owner(self.db.list_projects(), cat) if cat else None
            return self._category_taken(profile, asked, cat, owner)
        for i, m in enumerate(profile.get("milestones") or []):
            self.db.save_milestone(Milestone(project_id=project.id, title=m["title"], position=i,
                                             due=date.fromisoformat(m["due"]) if m.get("due") else None))
        n = project_service.attach_open_tasks(self.db, project)
        self.db.clear_draft()
        msg = f"✅ <b>Projet créé</b> : {fmt.esc(project.name)}"
        if cat:
            msg += f"\n📎 {n} tâche{'s' if n > 1 else ''} déjà rattachée{'s' if n > 1 else ''} à la catégorie " \
                   f"<code>{fmt.esc(cat)}</code>"
        return [(msg + "\n<i>/projets pour suivre l'avancement.</i>", [[("📂 Voir le projet", f"pjv:{project.id}")]])]

    def _category_taken(self, profile: dict, asked: int, cat: str | None, owner: Project | None) -> list[Msg]:
        profile["category"] = None
        profile["skipped"] = [k for k in profile.get("skipped", []) if k != "category"]
        who = f" au projet <b>{fmt.esc(owner.name)}</b>" if owner else " à un autre projet"
        warn = (f"⚠️ La catégorie <code>{fmt.esc(cat or '')}</code> appartient déjà{who}.", [])
        return [warn] + self._advance(profile, asked, force="category")
