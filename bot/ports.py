"""Ports (Protocols) vers les modules écrits en parallèle : coach, router, mémoire.

Les imports réels sont tolérants : si un module n'est pas prêt, l'adaptateur
lève ``NotReadyError`` à l'appel (jamais à l'import).
"""
from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

Sensitivity = Literal["private", "public"]


class NotReadyError(RuntimeError):
    """Le module cible n'est pas (encore) disponible."""


@dataclass
class Answer:
    text: str
    sources: list[str] = field(default_factory=list)


@runtime_checkable
class RouterPort(Protocol):
    def complete(self, prompt: str, *, sensitivity: Sensitivity, system: str | None = None,
                 json_mode: bool = False) -> str: ...

    def transcribe(self, audio_path: str) -> str: ...


@runtime_checkable
class CoachPort(Protocol):
    """Extraction + stockage/actions sur les tâches (coach.extract / coach.store)."""

    def extract_items(self, text: str, source_id: str, sensitivity: Sensitivity) -> tuple[list[Any], list[Any]]: ...
    def add_event(self, event_id: str) -> str: ...
    def ignore_event(self, event_id: str) -> str: ...
    def list_open_tasks(self) -> list[Any]: ...
    def today_agenda(self) -> str: ...
    def weekly_recap(self) -> str: ...
    def agenda_context(self) -> str: ...
    def mark_done(self, task_id: str) -> bool: ...
    def schedule(self, task_id: str) -> str: ...  # texte de confirmation (créneau choisi)
    def set_duration(self, task_id: str, minutes: int) -> str | None: ...  # texte du nouveau créneau si replacée
    def ignore(self, task_id: str) -> None: ...
    def report_outcome(self, block_id: str, outcome: str) -> str: ...  # done|not_finished|rescheduled|next_week
    def postpone_task(self, task_id: str, when: str) -> str: ...  # when : tomorrow|next_week|AAAA-MM-JJ
    def resolve(self, action: str, task_id: str) -> str: ...  # split|drop
    def replan_all(self) -> str: ...
    def focus(self) -> tuple[str, list[list[tuple[str, str]]]]: ...  # (texte HTML, boutons)
    def adjust_importance(self, task_id: str, up: bool) -> tuple[str, list[list[tuple[str, str]]]]: ...
    def start_now(self, task_id: str) -> str: ...
    def interview_active(self) -> bool: ...
    def interview_start(self) -> list[tuple[str, list[list[tuple[str, str]]]]]: ...
    def interview_text(self, text: str) -> list[tuple[str, list[list[tuple[str, str]]]]]: ...
    def interview_callback(self, rest: str) -> list[tuple[str, list[list[tuple[str, str]]]]]: ...
    def interview_cancel(self) -> list[tuple[str, list[list[tuple[str, str]]]]]: ...
    def task_folders(self) -> list[list[tuple[str, str]]]: ...
    def project_tasks_view(self, project_id: str) -> tuple[str, list[list[tuple[str, str]]]]: ...
    def projects_overview(self) -> tuple[str, list[list[tuple[str, str]]]]: ...
    def project_action(self, kind: str, rest: str) -> tuple[str, list[list[tuple[str, str]]]]: ...
    def project_pick(self, action: str, task_id: str, arg: str | None) -> tuple[str, list[list[tuple[str, str]]]]: ...
    def project_proposal(self, task_id: str) -> tuple[str, list[list[tuple[str, str]]]] | None: ...
    def assistant_action(self, kind: str, rest: str) -> tuple[str, list[list[tuple[str, str]]]]: ...


@runtime_checkable
class MemoryPort(Protocol):
    """Second-brain : notes, documents, recherche."""

    def add_note(self, text: str, sensitivity: Sensitivity, source: str) -> str: ...
    def add_document(self, path: str, filename: str, sensitivity: Sensitivity, kind: str) -> str: ...
    def set_sensitivity(self, doc_id: str, sensitivity: Sensitivity) -> None: ...
    def ask(self, question: str) -> Answer: ...


class _LazyModule:
    def __init__(self, name: str) -> None:
        self._name = name

    def __getattr__(self, item: str) -> Any:
        try:
            mod = importlib.import_module(self._name)
        except ImportError as exc:
            raise NotReadyError(f"module {self._name} indisponible") from exc
        try:
            return getattr(mod, item)
        except AttributeError as exc:
            raise NotReadyError(f"{self._name}.{item} indisponible") from exc


class RouterAdapter:
    """Délègue à llm_router.router (import paresseux)."""

    def __init__(self) -> None:
        self._m = _LazyModule("llm_router.router")

    def complete(self, prompt: str, *, sensitivity: Sensitivity, system: str | None = None,
                 json_mode: bool = False) -> str:
        return self._m.complete(prompt, sensitivity=sensitivity, system=system, json_mode=json_mode)

    def transcribe(self, audio_path: str) -> str:
        return self._m.transcribe(audio_path)


class CoachAdapter:
    """coach.extract + coach.store (noms de fonctions du store supposés, cf. besoins)."""

    def __init__(self) -> None:
        self._extract = _LazyModule("coach.extract")
        self._store = _LazyModule("coach.store")

    def extract_items(self, text: str, source_id: str, sensitivity: Sensitivity) -> tuple[list[Any], list[Any]]:
        # le store persiste les tâches (sinon « Planifier » ne retrouverait pas l'id)
        return self._store.extract_items(text, source_id, sensitivity)

    def add_event(self, event_id: str) -> str:
        return self._store.add_event(event_id)

    def ignore_event(self, event_id: str) -> str:
        return self._store.ignore_event(event_id)

    def list_open_tasks(self) -> list[Any]:
        return self._store.list_open_tasks()

    def today_agenda(self) -> str:
        return self._store.today_agenda()

    def weekly_recap(self) -> str:
        return self._store.weekly_recap()

    def agenda_context(self) -> str:
        return self._store.agenda_context()

    def mark_done(self, task_id: str) -> bool:
        return bool(self._store.mark_done(task_id))

    def schedule(self, task_id: str) -> str:
        return self._store.schedule(task_id)

    def set_duration(self, task_id: str, minutes: int) -> str | None:
        return self._store.set_duration(task_id, minutes)

    def ignore(self, task_id: str) -> None:
        self._store.ignore(task_id)

    def report_outcome(self, block_id: str, outcome: str) -> str:
        return self._store.report_outcome(block_id, outcome)

    def postpone_task(self, task_id: str, when: str) -> str:
        return self._store.postpone_task(task_id, when)

    def resolve(self, action: str, task_id: str) -> str:
        return self._store.resolve(action, task_id)

    def replan_all(self) -> str:
        return self._store.replan_all()

    def focus(self) -> tuple[str, list[list[tuple[str, str]]]]:
        return self._store.focus()

    def adjust_importance(self, task_id: str, up: bool) -> tuple[str, list[list[tuple[str, str]]]]:
        return self._store.adjust_importance(task_id, up)

    def start_now(self, task_id: str) -> str:
        return self._store.start_now(task_id)

    def interview_active(self) -> bool:
        return self._store.interview_active()

    def interview_start(self) -> list[tuple[str, list[list[tuple[str, str]]]]]:
        return self._store.interview_start()

    def interview_text(self, text: str) -> list[tuple[str, list[list[tuple[str, str]]]]]:
        return self._store.interview_text(text)

    def interview_callback(self, rest: str) -> list[tuple[str, list[list[tuple[str, str]]]]]:
        return self._store.interview_callback(rest)

    def interview_cancel(self) -> list[tuple[str, list[list[tuple[str, str]]]]]:
        return self._store.interview_cancel()

    def task_folders(self) -> list[list[tuple[str, str]]]:
        return self._store.task_folders()

    def project_tasks_view(self, project_id: str) -> tuple[str, list[list[tuple[str, str]]]]:
        return self._store.project_tasks_view(project_id)

    def projects_overview(self) -> tuple[str, list[list[tuple[str, str]]]]:
        return self._store.projects_overview()

    def project_action(self, kind: str, rest: str) -> tuple[str, list[list[tuple[str, str]]]]:
        return self._store.project_action(kind, rest)

    def project_pick(self, action: str, task_id: str, arg: str | None) -> tuple[str, list[list[tuple[str, str]]]]:
        return self._store.project_pick(action, task_id, arg)

    def project_proposal(self, task_id: str) -> tuple[str, list[list[tuple[str, str]]]] | None:
        return self._store.project_proposal(task_id)

    def assistant_action(self, kind: str, rest: str) -> tuple[str, list[list[tuple[str, str]]]]:
        return self._store.assistant_action(kind, rest)


class MemoryAdapter:
    """Second-brain (nom du module d'import configurable)."""

    def __init__(self, module_name: str = "memory") -> None:
        self._m = _LazyModule(module_name)

    def add_note(self, text: str, sensitivity: Sensitivity, source: str) -> str:
        return self._m.add_note(text, sensitivity, source)

    def add_document(self, path: str, filename: str, sensitivity: Sensitivity, kind: str) -> str:
        return self._m.add_document(path, filename, sensitivity, kind)

    def set_sensitivity(self, doc_id: str, sensitivity: Sensitivity) -> None:
        self._m.set_sensitivity(doc_id, sensitivity)

    def describe(self, doc_id: str) -> tuple[str, list[tuple[str, str]]]:
        return self._m.describe(doc_id)

    def move(self, doc_id: str, folder_key: str) -> str:
        return self._m.move(doc_id, folder_key)

    def ask(self, question: str, context: str | None = None) -> Answer:
        res = self._m.ask(question, context=context)
        if isinstance(res, Answer):
            return res
        return Answer(text=str(getattr(res, "text", res)), sources=list(getattr(res, "sources", []) or []))
