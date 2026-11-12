import notion_checklist as nc
from coach.db import Database
from coach.models import Project, Task

V = "https://www.professormesser.com/security-plus/sy0-701/sy0-701-video/"


def box(url, checked):
    return {"id": "b" + url[-6:], "type": "to_do", "has_children": False,
            "to_do": {"checked": checked, "rich_text": [{"plain_text": "1.2 "}, {"plain_text": "x", "href": url}]}}


class Reader:
    def __init__(self, boxes):
        self.tree = {"page": [{"id": "tog", "type": "heading_3", "has_children": True},
                              {"id": "fiche", "type": "child_page", "has_children": True},     # jamais ouverte
                              {"id": "tab", "type": "table", "has_children": True}],
                     "tog": boxes, "fiche": [box(V + "change/", False)]}
        self.asked = []

    def children(self, block_id):
        self.asked.append(block_id)
        return self.tree.get(block_id, [])


def setup(statuses):
    db = Database(":memory:")
    p = Project(name="Security+")
    db.save_project(p)
    a = Task(title="1.2", project_id=p.id, description=f"Vidéo : {V}cia/\n   Fiche Notion : https://app.notion.com/p/abc\nVidéo : {V}aaa/")
    b = Task(title="1.3", project_id=p.id, description=f"Vidéo : {V}change/")
    other = Task(title="hors projet", description=f"Vidéo : {V}cia/")
    for t in (a, b, other):
        db.upsert_task(t)
    done = []
    r = Reader([box(V + "cia/", statuses[0]), box(V + "aaa", statuses[1]), box(V + "change/", statuses[2])])
    return db, r, done, (a, b, other)


def test_task_done_only_when_all_its_boxes_are_checked():
    db, r, done, (a, b, other) = setup((True, False, True))
    assert nc.sync(db, r, "page", done.append) == 1
    assert done == [b.id]                      # 1.2 : une case sur deux ; la tâche hors projet n'est pas touchée
    assert r.asked == ["page", "tog"]          # les cases sont dans un bloc repliable


def test_all_checked_and_already_done_tasks_are_skipped():
    db, r, done, (a, b, other) = setup((True, True, True))
    db.set_task_status(b.id, "done")
    assert nc.sync(db, r, "page", done.append) == 1 and done == [a.id]


def test_disabled_without_page(monkeypatch):
    monkeypatch.setenv("NOTION_TOKEN", "x")
    monkeypatch.delenv("NOTION_CHECKLIST_PAGE", raising=False)
    assert nc.config() is None
    monkeypatch.setenv("NOTION_CHECKLIST_PAGE", "413f")
    assert nc.config() == ("x", "413f")
