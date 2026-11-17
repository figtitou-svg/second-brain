"""Priorités : enjeu noté par l'IA (une fois par version de tâche), une tâche par domaine, Franck = agenda."""
import json
from datetime import datetime, timedelta

from coach import priorities as pr, store, work_quota as wq
from coach.calendar_client import FakeCalendarClient
from coach.db import Database
from coach.models import Project, Task
from coach.rules import load_rules
from coach.scheduler import CoachScheduler
from tests.coach.conftest import FROZEN

TZ = wq.TZ
NOW = datetime(2026, 11, 17, 9, 0, tzinfo=TZ)


def sched_env():
    db = Database(":memory:")
    s = CoachScheduler(db, FakeCalendarClient(), load_rules(FROZEN), lambda t, b=None: None, now_fn=lambda: NOW)
    store.configure(db, s)
    return db, s


def test_stakes_rated_once_and_redone_when_task_changes():
    db, s = sched_env()
    a = Task(title="[Cha] Mail aux alumni", due=NOW + timedelta(days=5))
    b = Task(title="[Esp] Fiche 3", due=NOW + timedelta(days=5))
    for t in (a, b):
        db.upsert_task(t)
    calls = []

    def llm(prompt, **k):
        calls.append(prompt)
        return json.dumps({a.id: {"s": 5, "r": "argent pour l'asso"}, b.id: {"s": 9, "r": "x"}})
    assert pr.rate(db, llm, db.list_tasks("pending"), {}, NOW) == 2
    assert "catégorie : Cha" in calls[0] and "De :" not in calls[0]
    assert pr.rate(db, llm, db.list_tasks("pending"), {}, NOW) == 0 and len(calls) == 1   # déjà notées
    assert pr.valid_stakes(db, [a, b]) == {a.id: (5, "argent pour l'asso"), b.id: (5, "x")}
    db.upsert_task(a.model_copy(update={"title": "[Cha] Mail aux alumni (relance)"}))
    assert [t.id for t in pr.to_rate(db, db.list_tasks("pending"))] == [a.id]


def test_rate_survives_llm_failure():
    db, s = sched_env()
    db.upsert_task(Task(title="A"))
    def boom(*a, **k):
        raise RuntimeError("quota")
    assert pr.rate(db, boom, db.list_tasks("pending"), {}, NOW) == 0


def test_stakes_reorder_and_one_task_per_domain():
    db, s = sched_env()
    p = Project(name="Security+")
    db.save_project(p)
    same_due = NOW + timedelta(days=3)
    sec = [Task(title=f"[Sec+] 1.{i}", project_id=p.id, due=same_due) for i in range(3)]
    cha = Task(title="[Cha] Vente gâteaux", due=same_due)
    for t in (*sec, cha):
        db.upsert_task(t)
    db.set_stakes(cha.id, 5, "engagement pris", pr.stakes_key(cha))
    doms = s.domains(NOW)
    assert [d.name for d in doms] == ["Cha", "Security+"]          # l'enjeu fait passer CHAPS devant
    assert len(doms[1].rest) == 2 and "engagement pris" in pr.why(doms[0].top)
    text, buttons = s.focus_card()
    assert "Si tu ne fais qu'une chose" in text and "Security+</b> (+2)" in text and buttons[0][0][1] == f"nudgego:{cha.id}"


def test_job_hours_are_agenda_only():
    db, s = sched_env()
    store.ensure_work_quota()
    assert all(not wq.is_quota(t) for t in db.list_tasks("pending", "scheduled"))       # pas dans les listes
    assert all(not wq.is_quota(r.task) for r in s.ranking(NOW))
    blk = min((b for b in db.list_blocks(open_only=True) if b.task_id.startswith(wq.PREFIX)), key=lambda b: b.start)
    sent = []
    s.notify = lambda text, buttons=None: sent.append((text, buttons))
    s.tick(blk.start - timedelta(minutes=10))
    assert any("Travail · Franck (Nomana IT)" in t and b is None for t, b in sent)
    sent.clear()
    s.tick(blk.end + timedelta(minutes=1))
    assert not any("C'est fait" in t for t, _ in sent)
    assert next(b for b in db.list_blocks() if b.id == blk.id).outcome == "done"


def test_project_proposes_first_open_task_of_current_step_not_the_exam():
    from coach.models import Milestone
    db, s = sched_env()
    p = Project(name="Security+")
    db.save_project(p)
    d2 = Milestone(project_id=p.id, title="Domaine 2", position=1)
    d3 = Milestone(project_id=p.id, title="Domaine 3", position=2)
    fin = Milestone(project_id=p.id, title="Entraînement final et examen", position=5)
    for m in (d2, d3, fin):
        db.save_milestone(m)
    exam = Task(title="[Sec+] Passer l'examen Security+", project_id=p.id, milestone_id=fin.id, estimated_minutes=120)
    t24 = Task(title="[Sec+] 2.4 · Malwares (1/2)", project_id=p.id, milestone_id=d2.id, estimated_minutes=120)
    t23 = Task(title="[Sec+] 2.3 · Vulnérabilités (1/2)", project_id=p.id, milestone_id=d2.id, estimated_minutes=105)
    t31 = Task(title="[Sec+] 3.1 · Architecture", project_id=p.id, milestone_id=d3.id, estimated_minutes=30)
    cha = Task(title="[Cha] Vente gâteaux")
    for t in (exam, t24, t23, t31, cha):
        db.upsert_task(t)
    db.set_stakes(exam.id, 5, "certification", pr.stakes_key(exam))
    dom = next(d for d in s.domains(NOW) if d.name == "Security+")
    assert dom.top.task.id == t23.id                                  # 2.3 avant 2.4, l'examen en dernier
    assert [r.task.id for r in dom.rest] == [t24.id, t31.id, exam.id]


def test_step_without_numbering_takes_shortest_first():
    from coach.models import Milestone
    db, s = sched_env()
    p = Project(name="Cyber")
    db.save_project(p)
    m = Milestone(project_id=p.id, title="Attaque", position=0)
    db.save_milestone(m)
    long_ = Task(title="[Cyber] Notion : Reverse shell", project_id=p.id, milestone_id=m.id, estimated_minutes=90)
    short = Task(title="[Cyber] Notion : DNS", project_id=p.id, milestone_id=m.id, estimated_minutes=20)
    for t in (long_, short):
        db.upsert_task(t)
    assert next(d for d in s.domains(NOW) if d.name == "Cyber").top.task.id == short.id
