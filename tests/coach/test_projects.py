from __future__ import annotations

from datetime import date, timedelta

from coach import projects
from coach.models import Milestone, Project, Task
from tests.coach.conftest import dt

NOW = dt(2026, 10, 20, 10)
SUBJECTS = {"Stage 3IF": "Stg", "Algorithmique avancée pour l'IA et les graphes": "IA",
            "Génie logiciel et modélisation UML": "GL"}


def test_resolve_category():
    assert projects.resolve_category("Stage 3IF", SUBJECTS) == "Stg"
    assert projects.resolve_category("stage 3if", SUBJECTS) == "Stg"
    assert projects.resolve_category("génie logiciel et modélisation uml", SUBJECTS) == "GL"
    assert projects.resolve_category("ia", SUBJECTS) == "IA"
    assert projects.resolve_category("[Robotique]", SUBJECTS) == "Robotique"
    assert projects.resolve_category("   ", SUBJECTS) is None
    assert projects.resolve_category("Rugby", SUBJECTS, extra_codes=("rugby",)) == "rugby"


def test_category_owner_ignores_finished_projects():
    a = Project(name="A", category="IA", status="done")
    b = Project(name="B", category="GL", status="paused")
    assert projects.category_owner([a, b], "ia") is None
    assert projects.category_owner([a, b], "gl").name == "B"
    assert projects.category_owner([b], "GL", exclude_id=b.id) is None


def test_auto_project_by_prefix():
    active = Project(name="IA", category="IA")
    paused = Project(name="GL", category="GL", status="paused")
    done = Project(name="Vieux", category="BD", status="done")
    ps = [active, paused, done]
    assert projects.auto_project(Task(title="[IA] TP"), ps).id == active.id
    assert projects.auto_project(Task(title="[ia] TP"), ps).id == active.id
    assert projects.auto_project(Task(title="[GL] UML"), ps).id == paused.id   # la pause garde le rattachement
    assert projects.auto_project(Task(title="[BD] x"), ps) is None              # terminé
    assert projects.auto_project(Task(title="sans préfixe"), ps) is None


def _fixture():
    p = Project(name="Projet IA", category="IA", deadline=date(2026, 12, 15),
                last_activity=NOW - timedelta(days=2))
    m1 = Milestone(project_id=p.id, title="Données", position=0, status="done")
    m2 = Milestone(project_id=p.id, title="Modèle", position=1)
    m3 = Milestone(project_id=p.id, title="Rapport", position=2)
    tasks = [
        Task(title="[IA] a", project_id=p.id, milestone_id=m1.id, status="done"),
        Task(title="[IA] b", project_id=p.id, milestone_id=m2.id, status="done"),
        Task(title="[IA] c", project_id=p.id, milestone_id=m2.id, status="scheduled", due=dt(2026, 10, 28, 18)),
        Task(title="[IA] d", project_id=p.id, milestone_id=m3.id, status="pending", due=dt(2026, 10, 25, 18)),
        Task(title="[IA] e", project_id=p.id, milestone_id=m2.id, status="dropped"),
    ]
    return p, [m1, m2, m3], tasks


def test_progress():
    p, ms, tasks = _fixture()
    prog = projects.progress(p, ms, tasks, NOW)
    assert (prog.done_milestones, prog.total_milestones) == (1, 3)
    assert prog.current.title == "Modèle"
    assert (prog.current_done, prog.current_total) == (1, 2)   # la tâche abandonnée est ignorée
    assert (prog.tasks_done, prog.tasks_total) == (2, 4)
    assert prog.next_task.title == "[IA] c"                    # étape en cours d'abord, même si d est plus proche
    assert prog.days_left == 56 and prog.last_activity == p.last_activity


def test_progress_without_milestones_and_without_open_task():
    p = Project(name="P")
    prog = projects.progress(p, [], [Task(title="x", project_id=p.id, status="done")], NOW)
    assert (prog.total_milestones, prog.current, prog.next_task, prog.days_left) == (0, None, None, None)
    assert (prog.tasks_done, prog.tasks_total) == (1, 1)


def test_progress_when_all_milestones_are_done():
    p = Project(name="P")
    m = Milestone(project_id=p.id, title="Seule", status="done")
    prog = projects.progress(p, [m], [], NOW)
    assert prog.current is None and prog.done_milestones == 1


def test_milestone_ready():
    p = Project(name="P")
    m = Milestone(project_id=p.id, title="M")
    done = Task(title="a", milestone_id=m.id, status="done")
    open_ = Task(title="b", milestone_id=m.id, status="pending")
    dropped = Task(title="c", milestone_id=m.id, status="dropped")
    assert projects.milestone_ready(m, [done, dropped]) is True
    assert projects.milestone_ready(m, [done, open_]) is False
    assert projects.milestone_ready(m, []) is False
    assert projects.milestone_ready(m, [dropped]) is False


def test_bar():
    assert projects.bar(1, 3) == "▓▓░░░░"
    assert projects.bar(0, 0) == "░░░░░░"
    assert projects.bar(3, 3) == "▓▓▓▓▓▓"
    assert projects.bar(1, 100) == "▓░░░░░"        # commencé : au moins une case
    assert projects.bar(1, 20) == "▓░░░░░"
    assert projects.bar(11, 12) == "▓▓▓▓▓░"        # pas fini : jamais pleine
    assert projects.bar(5, 3) == "▓▓▓▓▓▓" and projects.bar(0, 5) == "░░░░░░"


def test_detail_card_is_bounded_with_many_open_tasks():
    p = Project(name="Gros projet", category="IA")
    ms = [Milestone(project_id=p.id, title=f"Étape {i}", position=i) for i in range(3)]
    tasks = [Task(title=f"[IA] tâche numéro {i} " + "x" * 40, project_id=p.id,
                  milestone_id=ms[i % 3].id if i < 60 else None, status="pending") for i in range(80)]
    prog = projects.progress(p, ms, tasks, NOW)
    text = projects.detail_card(p, ms, tasks, prog, NOW)
    assert len(text) <= 4000
    # 60 tâches réparties sur 3 étapes (20 chacune) : 15 masquées par étape ; 20 sans étape : 15 masquées
    assert text.count("      … et 15 autres") == 4
    assert text.count("      • ") == 20


def test_detail_card_whole_line_guard_cuts_at_4000():
    p = Project(name="Gros projet")
    ms = [Milestone(project_id=p.id, title="E" * 70 + str(i), position=i) for i in range(80)]
    tasks = [Task(title="t" * 200 + str(i), project_id=p.id, milestone_id=ms[i].id, status="pending")
             for i in range(80)]
    prog = projects.progress(p, ms, tasks, NOW)
    text = projects.detail_card(p, ms, tasks, prog, NOW)
    assert len(text) <= 4000 and text.endswith("\n… (liste tronquée)")
    assert text.split("\n")[0].startswith("📁")                  # on coupe des lignes entières, jamais au milieu


def test_overview_card_content_and_escaping():
    p, ms, tasks = _fixture()
    p = p.model_copy(update={"name": "Projet <IA> & co"})
    text = projects.overview_card(p, projects.progress(p, ms, tasks, NOW), NOW)
    assert "Projet &lt;IA&gt; &amp; co" in text
    assert "Étape 2/3" in text and "<i>Modèle</i>" in text and "1/2 tâches" in text and "▓▓░░░░" in text
    assert "dans 56 j" in text and "[IA] c" in text and "il y a 2 j" in text


def test_overview_card_without_milestones_and_late_deadline():
    p = Project(name="Perso", deadline=date(2026, 10, 18))
    text = projects.overview_card(p, projects.progress(p, [], [], NOW), NOW)
    assert "en retard de 2 j" in text and "0/0 tâches" in text and "Aucune tâche ouverte" in text
    assert "aucune activité" in text


def test_detail_card_lists_milestones_and_open_tasks():
    p, ms, tasks = _fixture()
    p = p.model_copy(update={"profile": {"goal": "Entraîner un modèle", "limits": "pas de deep learning"}})
    text = projects.detail_card(p, ms, tasks, projects.progress(p, ms, tasks, NOW), NOW)
    assert "Entraîner un modèle" in text and "pas de deep learning" in text
    assert "✅ Données" in text and "⏳ Modèle" in text and "⏳ Rapport" in text
    assert "[IA] c" in text and "[IA] a" not in text   # seules les tâches ouvertes sont listées
