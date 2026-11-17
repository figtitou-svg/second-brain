"""Regroupe les tâches [Job] de recherche de stage (envoi ou relance de mails, prise de contact, candidatures)
dans un projet « Stage 2027 ».

    python -m scripts.stage_2027                 # aperçu : liste ce qui serait rattaché, ne modifie rien
    python -m scripts.stage_2027 --apply         # crée le projet (s'il n'existe pas) et rattache les tâches
    python -m scripts.stage_2027 --exclude ID --include ID --apply   # corriger la sélection à la main

À lancer sur la VM, dans le dossier du projet, service arrêté ou non (SQLite gère les accès concurrents).
Seules les tâches ouvertes (à faire ou planifiées) de catégorie Job sont considérées ; une tâche déjà dans un
autre projet n'est pas déplacée sauf si elle est passée avec --include. Relancer le script ne crée pas de doublon.
"""
from __future__ import annotations

import argparse
import re

from coach.db import DEFAULT_DB_PATH, Database
from coach.focus import subject_of
from coach.models import Project, Task
from coach.projects import fold

PROJECT_NAME = "Stage 2027"
CATEGORY = "job"
# mots (sans accents, en minuscules) qui signalent un mail, une relance, un contact ou une candidature de stage
KEYWORDS = re.compile(
    r"\b(e-?mails?|mails?|courriels?|relanc\w*|contact\w*|candidat\w*|postul\w*|stages?|recrut\w*|"
    r"entreprises?|lettres? de motivation|cv|linkedin|alternance|offres?|entretiens?|rh)\b")


def is_stage_task(t: Task) -> bool:
    cat = subject_of(t.title)
    return cat is not None and fold(cat) == CATEGORY and bool(KEYWORDS.search(fold(t.title)))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=str(DEFAULT_DB_PATH))
    ap.add_argument("--apply", action="store_true", help="écrit dans la base (sinon : aperçu seulement)")
    ap.add_argument("--include", action="append", default=[], metavar="ID", help="ajoute cette tâche")
    ap.add_argument("--exclude", action="append", default=[], metavar="ID", help="retire cette tâche")
    args = ap.parse_args()

    db = Database(args.db)
    project = next((p for p in db.list_projects("active", "paused") if fold(p.name) == fold(PROJECT_NAME)), None)
    open_tasks = db.list_tasks("pending", "scheduled")
    jobs = [t for t in open_tasks if (c := subject_of(t.title)) and fold(c) == CATEGORY]
    chosen = [t for t in open_tasks
              if t.id not in args.exclude
              and (t.id in args.include
                   or (is_stage_task(t) and (t.project_id is None or (project and t.project_id == project.id))))]
    others = [t for t in jobs if t not in chosen]

    print(f"Projet « {PROJECT_NAME} » : {'existe déjà' if project else 'sera créé'}")
    print(f"\nTâches rattachées ({len(chosen)}) :")
    for t in chosen:
        print(f"  + {t.id}  {t.title}")
    print(f"\nAutres tâches Job laissées telles quelles ({len(others)}) :")
    for t in others:
        note = "  (déjà dans un autre projet)" if t.project_id and (not project or t.project_id != project.id) else ""
        print(f"  - {t.id}  {t.title}{note}")

    if not args.apply:
        print("\nAperçu seulement : rien n'a été modifié. Relance avec --apply pour appliquer "
              "(--exclude ID / --include ID pour corriger la liste).")
        return
    if project is None:
        project = Project(name=PROJECT_NAME)
        db.save_project(project)
    for t in chosen:
        db.set_task_project(t.id, project.id, None)
    print(f"\nFait : {len(chosen)} tâche(s) dans « {PROJECT_NAME} » ({project.id}).")


if __name__ == "__main__":
    main()
