from coach.db import Database
from coach.models import Task
from scripts import stage_2027


def test_selects_only_open_job_tasks_about_internship_contacts():
    yes = ["[Job] Relancer Thales par mail", "[job] Contacter le RH", "[Job] Envoyer candidature stage Airbus"]
    no = ["[Job] Faire le planning du McDo", "[Esp] Envoyer un mail au prof", "Relancer la banque"]
    assert all(stage_2027.is_stage_task(Task(title=t)) for t in yes)
    assert not any(stage_2027.is_stage_task(Task(title=t)) for t in no)


def test_apply_creates_project_once_and_attaches(tmp_path, monkeypatch):
    path = tmp_path / "c.sqlite"
    db = Database(path)
    a, b = Task(title="[Job] Relancer Thales par mail"), Task(title="[Job] Planning du McDo")
    db.upsert_task(a), db.upsert_task(b)
    for _ in range(2):
        monkeypatch.setattr("sys.argv", ["x", "--db", str(path), "--apply"])
        stage_2027.main()
    db = Database(path)
    [p] = db.list_projects()
    assert p.name == "Stage 2027" and db.get_task(a.id).project_id == p.id and db.get_task(b.id).project_id is None


def test_preview_changes_nothing(tmp_path, monkeypatch):
    path = tmp_path / "c.sqlite"
    Database(path).upsert_task(Task(title="[Job] Relancer Thales par mail"))
    monkeypatch.setattr("sys.argv", ["x", "--db", str(path)])
    stage_2027.main()
    assert Database(path).list_projects() == []
