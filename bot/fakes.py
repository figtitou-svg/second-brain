"""Fakes en mémoire pour les tests (aucun réseau)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .ports import Answer, Sensitivity


@dataclass
class FakeTask:
    id: str
    title: str
    estimated_minutes: int = 60
    status: str = "pending"
    due: object | None = None
    not_before: object | None = None
    project_id: str | None = None


class FakeRouter:
    def __init__(self, transcript: str = "texte transcrit") -> None:
        self.transcript = transcript
        self.transcribed: list[str] = []

    def complete(self, prompt: str, *, sensitivity: Sensitivity, system: str | None = None,
                 json_mode: bool = False) -> str:
        return ""

    def transcribe(self, audio_path: str) -> str:
        self.transcribed.append(audio_path)
        return self.transcript


class FakeCoach:
    def resolve(self, action: str, task_id: str) -> str:
        self.calls.append(("resolve", action, task_id))
        return f"{action} ok"

    def __init__(self, extracted: list[Any] | None = None) -> None:
        self.extracted = extracted or []
        self.extract_calls: list[tuple[str, str, str]] = []
        self.tasks: dict[str, FakeTask] = {}
        self.calls: list[tuple] = []
        self.interview_open = False
        self.interview_replies: list[tuple[str, list]] = [("réponse de l'entretien", [[("Passer", "pi:skip")]])]
        self.proposals: dict[str, tuple[str, list]] = {}

    def extract_tasks(self, text: str, source_id: str, sensitivity: Sensitivity) -> list[Any]:
        self.extract_calls.append((text, source_id, sensitivity))
        for t in self.extracted:
            self.tasks[t.id] = t
        return list(self.extracted)

    def extract_items(self, text: str, source_id: str, sensitivity: Sensitivity) -> tuple[list[Any], list[Any]]:
        return self.extract_tasks(text, source_id, sensitivity), list(getattr(self, "events", []))

    def add_event(self, event_id: str) -> str:
        self.calls.append(("add_event", event_id))
        return f"Ajouté à l'agenda : {event_id}"

    def ignore_event(self, event_id: str) -> str:
        self.calls.append(("ignore_event", event_id))
        return "Événement ignoré."

    def list_open_tasks(self) -> list[Any]:
        return [t for t in self.tasks.values() if t.status in ("pending", "scheduled")]

    def today_agenda(self) -> str:
        return "Programme du jour : rien"

    def weekly_recap(self) -> str:
        return "Récap de la semaine : rien"

    def agenda_context(self) -> str:
        return "Date et heure actuelles : jeudi 15 octobre 2026, 14:00."

    def mark_done(self, task_id: str) -> bool:
        t = self.tasks.get(task_id)
        if not t:
            return False
        t.status = "done"
        return True

    def schedule(self, task_id: str) -> str:
        self.calls.append(("schedule", task_id))
        self.tasks[task_id].status = "scheduled"
        return "Planifié demain 10:00"

    def set_duration(self, task_id: str, minutes: int) -> str | None:
        self.calls.append(("duration", task_id, minutes))
        self.tasks[task_id].estimated_minutes = minutes
        return "Replanifié demain 11:00" if self.tasks[task_id].status == "scheduled" else None

    def ignore(self, task_id: str) -> None:
        self.calls.append(("ignore", task_id))
        self.tasks[task_id].status = "dropped"

    def report_outcome(self, block_id: str, outcome: str) -> str:
        self.calls.append(("outcome", block_id, outcome))
        return f"Noté : {outcome}"

    def postpone_task(self, task_id: str, when: str) -> str:
        self.calls.append(("postpone_task", task_id, when))
        return f"Reporté : {task_id} ({when})"


    def replan_all(self) -> str:
        self.calls.append(("replan_all",))
        return "Planning refait"

    def focus(self) -> tuple[str, list[list[tuple[str, str]]]]:
        self.calls.append(("focus",))
        return "🎯 Ton focus", [[("✅ 1", "td:t1"), ("⬆ 1", "fup:t1"), ("⬇ 1", "fdn:t1")]]

    def adjust_importance(self, task_id: str, up: bool) -> tuple[str, list[list[tuple[str, str]]]]:
        self.calls.append(("adjust", task_id, up))
        return f"ajusté {task_id}", [[("✅ 1", f"td:{task_id}")]]

    def start_now(self, task_id: str) -> str:
        self.calls.append(("start_now", task_id))
        return f"▶️ Placé : {task_id}"

    def interview_active(self) -> bool:
        return self.interview_open

    def interview_start(self) -> list[tuple[str, list]]:
        self.calls.append(("interview_start",))
        return [("🆕 <b>Nouveau projet</b>", [[("Annuler", "pi:cancel")]])]

    def interview_text(self, text: str) -> list[tuple[str, list]]:
        self.calls.append(("interview_text", text))
        return list(self.interview_replies)

    def interview_callback(self, rest: str) -> list[tuple[str, list]]:
        self.calls.append(("interview_callback", rest))
        return list(self.interview_replies)

    def interview_cancel(self) -> list[tuple[str, list]]:
        self.calls.append(("interview_cancel",))
        return [("🗑 Entretien annulé.", [])]

    def task_folders(self) -> list:
        return [[(f"📂 {name} ({sum(1 for t in self.list_open_tasks() if t.project_id == pid)})", f"ptk:{pid}")]
                for pid, name in getattr(self, "folders", {}).items()
                if any(t.project_id == pid for t in self.list_open_tasks())]

    def project_tasks_view(self, project_id: str) -> tuple[str, list]:
        open_ = [t for t in self.list_open_tasks() if t.project_id == project_id]
        return (f"📂 {project_id} ({len(open_)})",
                [[(f"✅ {t.title}", f"ptd:{project_id}:{t.id}"), ("⏭", f"ptp:{project_id}:{t.id}"),
                  ("📅", f"ptw:{project_id}:{t.id}")] for t in open_] + [[("⬅ Tâches", "ptl")]])

    def projects_overview(self) -> tuple[str, list]:
        self.calls.append(("projects_overview",))
        return "📁 <b>Tes projets</b>", [[("📂 Projet IA", "pjv:abcd1234")]]

    def project_action(self, kind: str, rest: str) -> tuple[str, list]:
        self.calls.append(("project_action", kind, rest))
        return f"action {kind}:{rest}", []

    def project_pick(self, action: str, task_id: str, arg: str | None) -> tuple[str, list]:
        self.calls.append(("project_pick", action, task_id, arg))
        return f"pick {action}", []

    def project_proposal(self, task_id: str):
        return self.proposals.get(task_id)

    def assistant_action(self, kind: str, rest: str) -> tuple[str, list]:
        self.calls.append(("assistant_action", kind, rest))
        return f"assistant {kind}", [[("ok", "pjv:x")]]


class FakeMemory:
    def __init__(self, answer: Answer | None = None) -> None:
        self.answer = answer or Answer("réponse", ["note:1"])
        self.notes: list[tuple[str, str, str]] = []
        self.docs: dict[str, dict[str, Any]] = {}
        self.questions: list[str] = []

    def add_note(self, text: str, sensitivity: Sensitivity, source: str) -> str:
        self.notes.append((text, sensitivity, source))
        return f"note{len(self.notes)}"

    def add_document(self, path: str, filename: str, sensitivity: Sensitivity, kind: str) -> str:
        doc_id = f"doc{len(self.docs) + 1}"
        self.docs[doc_id] = {"path": path, "filename": filename, "sensitivity": sensitivity, "kind": kind}
        return doc_id

    def set_sensitivity(self, doc_id: str, sensitivity: Sensitivity) -> None:
        self.docs[doc_id]["sensitivity"] = sensitivity

    def ask(self, question: str, context: str | None = None) -> Answer:
        self.questions.append(question)
        self.contexts = getattr(self, "contexts", []) + [context]
        return self.answer
