from __future__ import annotations

from coach import project_assistant
from coach.models import Suggestion, Task
from tests.coach.assistant_helpers import NOW, FakeLLM, make_project

LONG = "t" * 36


def all_callbacks(buttons):
    return [cb for row in buttons for _, cb in row]


def test_every_assistant_callback_fits_in_64_bytes():
    db, p, m1, m2, m3 = make_project()
    items = [Suggestion(batch="lot00001", project_id=p.id, milestone_id=m2.id, title=f"Suggestion {c} distincte",
                        created_at=NOW) for c in ("Alpha", "Bravo", "Charlie", "Delta", "Echo")]
    db.save_suggestions(items)
    seen = all_callbacks(project_assistant.render_batch(db, "lot00001")[1])
    seen += all_callbacks(project_assistant.click(db, "add", items[0].id, NOW)[1])
    # rangement : une tâche à l'identifiant de 36 caractères
    task = Task(id=LONG, title="[IA] Entraîner le réseau", project_id=p.id)
    db.upsert_task(task)
    text, buttons = project_assistant.rank_batch(
        db, FakeLLM({"assign": [{"n": 1, "step": "Modèle"}]}), "private", p.id, NOW)
    seen += all_callbacks(buttons)
    seen += all_callbacks(project_assistant.rank_click(db, "ok", LONG, NOW)[1])
    assert any(c.startswith("ps:") for c in seen) and any(c.startswith("pa:ok:") for c in seen)
    assert all(len(c.encode()) <= 64 for c in seen), [c for c in seen if len(c.encode()) > 64]
