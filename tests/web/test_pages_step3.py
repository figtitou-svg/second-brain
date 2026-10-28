# tests/web/test_pages_step3.py
from __future__ import annotations

import logging
import re
import sys
import types

from coach.models import Event, Project, Task
from tests.coach.conftest import dt
from tests.web.test_data_step3 import FakeNotes
from tests.web.test_pages import NOW, make
from tests.web.test_server import csrf_of, login, run
from web import data

NEW = ["/a-trier", "/suggestions", "/notifications", "/notes"]


def test_new_pages_require_login_and_render_on_empty_db():
    app, _ = make(notes=None)

    async def scenario(c):
        for url in NEW + [f"/fragment/{u[1:]}" for u in NEW] + ["/notes/x", "/fragment/notes/x"]:
            assert (await c.get(url, allow_redirects=False)).status == 302, url
        await login(c)
        for url in NEW:
            r = await c.get(url)
            text = await r.text()
            assert r.status == 200 and "indisponible" not in text, url
            assert f'data-refresh="/fragment/{url[1:]}' in text, url
            assert "default-src 'self'" in r.headers.get("Content-Security-Policy", ""), url
            assert r.headers.get("Cache-Control") == "no-store" and r.headers.get("X-Content-Type-Options") == "nosniff"
            frag = await c.get(f"/fragment/{url[1:]}")
            assert frag.status == 200 and 'id="nav"' not in await frag.text(), url
            assert "default-src 'self'" in frag.headers.get("Content-Security-Policy", ""), url
    run(app, scenario)


def test_menu_has_no_soon_entries_left_and_new_links():
    app, _ = make()

    async def scenario(c):
        await login(c)
        page = await (await c.get("/a-trier")).text()
        for href in NEW:
            assert f'href="{href}"' in page, href
        assert "bientôt" not in page and 'aria-current="page"' in page
        assert re.findall(r'href="([^"]+)"[^>]*aria-current="page"', page) == ["/a-trier"]
        bottom = page.split('class="bottom"')[1]                     # barre basse inchangée
        assert re.findall(r'href="([^"]+)"', bottom) == ["/", "/agenda", "/taches", "/discussion"]
        notif = await (await c.get("/notifications")).text()
        assert re.findall(r'href="([^"]+)"[^>]*aria-current="page"', notif) == ["/notifications"]
    run(app, scenario)


def test_note_page_404_and_query_cleaning():
    app, _ = make(notes=FakeNotes())

    async def scenario(c):
        await login(c)
        r = await c.get("/notes/inconnu")
        text = await r.text()
        assert r.status == 404 and "Note introuvable" in text and "Second Brain" in text and 'id="nav"' in text
        r = await c.get("/fragment/notes/inconnu")
        assert r.status == 404 and 'id="nav"' not in await r.text()
        r = await c.get("/notes?q=%3Cscript%3Ealert(1)%3C/script%3E")
        text = await r.text()
        assert r.status == 200 and "<script>alert" not in text
        assert 'data-refresh="/fragment/notes?q=%3Cscript%3Ealert%281%29%3C/script%3E"' in text
    run(app, scenario)


def test_notes_search_refresh_url_uses_the_cleaned_query():
    app, _ = make(notes=FakeNotes())

    async def scenario(c):
        await login(c)
        text = await (await c.get("/notes?q=%20%20code%0A%0Agarage%20" + "x" * 300)).text()
        refresh = re.search(r'data-refresh="([^"]*)"', text).group(1)
        assert refresh.startswith("/fragment/notes?q=code%20garage%20x") and "%0A" not in refresh
        assert len(refresh) - len("/fragment/notes?q=") == 200 + 2 * 2                # 200 caractères, 2 espaces encodés
        plain = await (await c.get("/notes?q=")).text()
        assert 'data-refresh="/fragment/notes"' in plain and "Code garage" in plain and "Stage" in plain
        found = await (await c.get("/notes?q=garage")).text()
        assert "Code garage" in found and ">Stage<" not in found
        frag = await (await c.get("/fragment/notes?q=garage")).text()
        assert "Code garage" in frag and 'id="nav"' not in frag
    run(app, scenario)


def test_note_page_renders_escaped_body_and_marks_notes_current():
    notes = FakeNotes()
    notes.docs["n3"] = dict(notes.docs["n1"], id="n3", title="<b>Titre</b>", body="<script>x</script> texte")
    app, _ = make(notes=notes)

    async def scenario(c):
        await login(c)
        r = await c.get("/notes/n1")
        text = await r.text()
        assert r.status == 200 and "Code garage" in text and "Le code est 4721." in text
        assert 'data-refresh="/fragment/notes/n1"' in text
        assert 'href="/notes" title="Notes et documents" aria-current="page"' in text
        frag = await (await c.get("/fragment/notes/n1")).text()
        assert "Le code est 4721." in frag and 'id="nav"' not in frag
        evil = await (await c.get("/notes/n3")).text()
        assert "<b>Titre</b>" not in evil and "<script>x" not in evil and "&lt;script&gt;x" in evil
    run(app, scenario)


def test_note_routes_without_provider_or_views():
    app, _ = make(notes=None)

    async def scenario(c):
        await login(c)
        assert (await c.get("/notes/n1")).status == 404
    run(app, scenario)

    app, _ = make(views=False)

    async def scenario2(c):
        await login(c)
        for url in NEW + ["/notes/n1"]:
            r = await c.get(url)
            text = await r.text()
            assert r.status == 200 and "indisponible" in text and 'id="nav"' in text, url
            assert 'class="badge"' not in text, url
    run(app, scenario2)


def test_badges_show_counts_and_alert_dot_but_only_on_full_pages():
    app, db = make()
    db.add_event_if_new(Event(title="RDV", start=dt(2026, 10, 30, 9), end=dt(2026, 10, 30, 10)))
    db.upsert_task(Task(title="À planifier"))

    async def scenario(c):
        await login(c)
        page = await (await c.get("/agenda")).text()
        assert page.count('class="badge"') >= 2                      # À trier et Tâches
        assert re.search(r'href="/a-trier"[^>]*>.*?<b class="badge" aria-hidden="true">2</b>'
                         r'<span class="sr-only">\(2 à trier\)</span>', page, re.S)
        assert re.search(r'href="/taches"[^>]*>.*?<b class="badge" aria-hidden="true">1</b>'
                         r'<span class="sr-only">\(1 tâche ouverte\)</span>', page, re.S)
        assert 'class="dot"' not in page                              # aucun projet signalé
        frag = await (await c.get("/fragment/agenda")).text()
        assert 'class="badge"' not in frag
    run(app, scenario)


def test_project_alert_shows_an_accessible_dot_on_projects():
    app, db = make()
    db.save_project(Project(name="Stagne", last_activity=NOW - __import__("datetime").timedelta(days=12)))

    async def scenario(c):
        await login(c)
        page = await (await c.get("/")).text()
        entry = page.split('href="/projets"')[1].split("</a>")[0]
        assert '<i class="dot" role="img" aria-label="Un projet est signalé"></i>' in entry
    run(app, scenario)


def test_badges_failure_keeps_the_page():
    app, _ = make(break_badges=True)

    async def scenario(c):
        await login(c)
        r = await c.get("/agenda")
        text = await r.text()
        assert r.status == 200 and "Agenda" in text and 'class="badge"' not in text
        assert 'id="nav"' in text
    run(app, scenario)


def test_badges_are_cached_and_invalidated_by_actions(monkeypatch):
    calls = []
    original = data.Views.badges

    def counting(self):
        calls.append(1)
        return original(self)
    monkeypatch.setattr(data.Views, "badges", counting)
    t = [1000.0]
    app, db = make(badge_clock=lambda: t[0])
    db.upsert_task(Task(title="À planifier"))

    async def scenario(c):
        await login(c)
        await c.get("/agenda")
        await c.get("/taches")
        await c.get("/fragment/taches")                               # les fragments ne lisent pas les pastilles
        assert len(calls) == 1
        token = await csrf_of(c)                                      # GET / : encore dans le cache
        assert len(calls) == 1
        t[0] += 14.9
        await c.get("/agenda")
        assert len(calls) == 1
        t[0] += 0.2                                                   # 15 s écoulées : recalcul
        await c.get("/agenda")
        assert len(calls) == 2
        r = await c.post("/click", data={"data": "td:x"}, headers={"X-CSRF": token})
        assert r.status == 200 and len(calls) == 2                    # POST : pas de pastilles
        await c.get("/agenda")
        assert len(calls) == 3                                        # vidé par l'action
        await c.post("/say", data={"text": "/focus"}, headers={"X-CSRF": token})
        await c.get("/agenda")
        assert len(calls) == 4
        await c.get("/agenda")
        assert len(calls) == 4
    run(app, scenario)


def test_badge_numbers_follow_an_action_at_once(monkeypatch):
    app, db = make(badge_clock=lambda: 0.0)                           # horloge figée : seul le vidage recalcule
    t = Task(title="À planifier")
    db.upsert_task(t)

    async def scenario(c):
        await login(c)
        page = await (await c.get("/agenda")).text()
        assert "(1 tâche ouverte)" in page
        db.upsert_task(Task(title="Une autre"))
        assert "(1 tâche ouverte)" in await (await c.get("/agenda")).text()      # encore en cache
        token = await csrf_of(c)
        await c.post("/click", data={"data": "td:inconnu"}, headers={"X-CSRF": token})
        assert "(2 tâches ouvertes)" in await (await c.get("/agenda")).text()
    run(app, scenario)


def test_failing_badges_are_not_cached(monkeypatch):
    calls = []

    def boom(self):
        calls.append(1)
        raise RuntimeError("base verrouillée")
    monkeypatch.setattr(data.Views, "badges", boom)
    app, _ = make(badge_clock=lambda: 0.0)

    async def scenario(c):
        await login(c)
        assert (await c.get("/agenda")).status == 200
        assert (await c.get("/agenda")).status == 200
        assert len(calls) == 2                                        # une panne se réessaie au chargement suivant
    run(app, scenario)


def test_no_inline_scripts_in_new_pages():
    app, _ = make(notes=FakeNotes())

    async def scenario(c):
        await login(c)
        for url in NEW + ["/notes/n1", "/notes?q=code"]:
            text = await (await c.get(url)).text()
            assert not re.search(r"<[a-z][^>]*\son[a-z]+\s*=", text, re.I), url
            assert re.findall(r"<script[^>]*>", text) == ['<script src="/static/app.js" defer>'], url
    run(app, scenario)


# --- fournisseur de notes branché sur `memory` ------------------------------------------------------------


def test_memory_notes_calls_memory_lazily(monkeypatch):
    seen = []
    fake = types.ModuleType("memory")
    fake.recent_docs = lambda limit: seen.append(("recent", limit)) or [{"id": "a"}]
    fake.search_docs = lambda query, k: seen.append(("search", query, k)) or [{"id": "b"}]
    fake.get_doc = lambda doc_id: seen.append(("get", doc_id)) or {"id": doc_id}
    monkeypatch.setitem(sys.modules, "memory", fake)
    notes = data.MemoryNotes()
    assert notes.recent(50) == [{"id": "a"}]
    assert notes.search("code", 30) == [{"id": "b"}]
    assert notes.get("x1") == {"id": "x1"}
    assert seen == [("recent", 50), ("search", "code", 30), ("get", "x1")]


def test_memory_notes_never_raises(monkeypatch, caplog):
    def boom(*a, **k):
        raise RuntimeError("database disk image is malformed")
    fake = types.ModuleType("memory")
    fake.recent_docs = fake.search_docs = fake.get_doc = boom
    monkeypatch.setitem(sys.modules, "memory", fake)
    notes = data.MemoryNotes()
    with caplog.at_level(logging.WARNING):
        assert notes.recent(50) == [] and notes.search("x", 30) == [] and notes.get("x") is None
    assert caplog.text.count("notes :") == 3
    monkeypatch.setitem(sys.modules, "memory", None)                 # import impossible
    assert notes.recent(5) == [] and notes.search("x", 3) == [] and notes.get("x") is None


# --- Task 5 : gabarits À trier et Suggestions ---------------------------------------------------------------

from datetime import datetime, timedelta, timezone  # noqa: E402

from coach.models import Milestone, Suggestion  # noqa: E402

EVIL = '<script>alert("x")</script> "guillemets"'


def _buttons(html: str) -> list[tuple[str, str, str]]:
    """(data-cb, aria-label, texte visible sans les émojis aria-hidden) de chaque bouton."""
    out = []
    for attrs, inner in re.findall(r"<button([^>]*)>(.*?)</button>", html, re.S):
        cb = re.search(r'data-cb="([^"]*)"', attrs)
        label = re.search(r'aria-label="([^"]*)"', attrs)
        visible = re.sub(r'<span aria-hidden="true">.*?</span>', "", inner)
        visible = re.sub(r"<[^>]+>", "", visible).strip()
        out.append((cb.group(1) if cb else "", label.group(1) if label else "", visible))
    return out


def test_a_trier_page_buttons_once_escaped_and_labelled():
    app, db = make()
    db.add_event_if_new(Event(title=EVIL, start=datetime(2026, 10, 30, 8, tzinfo=timezone.utc),
                              end=datetime(2026, 10, 30, 9, 30, tzinfo=timezone.utc), location="Salle <b>B</b>"))
    ev = db.list_events("proposed")[0]
    late = Task(title=EVIL, due=NOW - timedelta(days=1), estimated_minutes=45)
    fine = Task(title="Plus tard", due=NOW + timedelta(days=3))
    for t in (late, fine):
        db.upsert_task(t)

    async def scenario(c):
        await login(c)
        for url in ("/a-trier", "/fragment/a-trier"):
            text = await (await c.get(url)).text()
            body = text.split('id="a-trier"')[1] if url == "/a-trier" else text
            assert "Rendez-vous détectés" in body and "Tâches à planifier" in body and "Rien à trier." not in body
            for cb in (f"evadd:{ev.id}", f"evign:{ev.id}", f"plan:{late.id}", f"dur:{late.id}", f"ign:{late.id}",
                       f"plan:{fine.id}"):
                assert body.count(f'data-cb="{cb}"') == 1, cb
            assert "<script>alert" not in body and "&lt;script&gt;" in body and "<b>B</b>" not in body
            assert "9h00" in body and "10h30" in body                         # heures locales (UTC+1)
            assert re.search(r'class="rowcard taskcard overdue"[^>]*>.*?En retard', body, re.S)
            for cb, label, visible in _buttons(body):
                assert visible and visible in label, (cb, label, visible)    # WCAG 2.5.3
    run(app, scenario)


def test_a_trier_empty_states():
    app, db = make()

    async def scenario(c):
        await login(c)
        body = (await (await c.get("/a-trier")).text()).split('id="a-trier"')[1]
        assert body.count("Rien à trier.") == 1 and "Rendez-vous détectés" not in body
        db.upsert_task(Task(title="Seule"))
        frag = await (await c.get("/fragment/a-trier")).text()
        assert frag.count("Rien à trier.") == 1 and "Rendez-vous détectés" in frag and "Seule" in frag
    run(app, scenario)


def test_suggestions_page_three_blocks_buttons_once_and_escaped():
    app, db = make()
    p = Project(name='Projet <i>IA</i> "x"', last_activity=NOW)
    db.save_project(p)
    m = Milestone(project_id=p.id, title="Étape <b>1</b>", position=0)
    db.save_milestone(m)
    sug = Suggestion(batch="b1", project_id=p.id, milestone_id=m.id, title=EVIL, estimated_minutes=40, created_at=NOW)
    db.save_suggestions([sug])
    loose = Task(title="Vrac <u>", project_id=p.id)
    db.upsert_task(loose)
    db.set_proposal(loose.id, p.id, m.id, "lot")
    other = Task(title="Nouvelle <script>")
    db.upsert_task(other)
    db.set_proposal(other.id, p.id, m.id, "proposed")

    async def scenario(c):
        await login(c)
        for url in ("/suggestions", "/fragment/suggestions"):
            text = await (await c.get(url)).text()
            body = text.split('id="suggestions"')[1] if url == "/suggestions" else text
            assert "Aucune suggestion en attente." not in body
            assert "Tâches proposées" in body and "Rangements" in body and "Propositions par tâche" in body
            for cb in (f"ps:add:{sug.id}", f"ps:plan:{sug.id}", f"ps:no:{sug.id}", "ps:all:b1", "ps:none:b1",
                       f"pa:ok:{loose.id}", f"pa:no:{loose.id}", f"pa:all:{p.id}", f"pa:none:{p.id}",
                       f"pjok:{other.id}", f"pjwho:{other.id}", f"pjno:{other.id}"):
                assert body.count(f'data-cb="{cb}"') == 1, cb
            assert "<script>" not in body and "<u>" not in body and "Projet <i>IA" not in body
            assert "Étape <b>1</b>" not in body
            assert "📁 <b>Projet &lt;i&gt;IA&lt;/i&gt;" in body                # texte Telegram filtré par tg
            assert "→" in body
            for cb, label, visible in _buttons(body):
                assert visible and visible in label, (cb, label, visible)
            labels = [label for _, label, _ in _buttons(body)]
            assert len(labels) == len(set(labels))                           # pas deux boutons au même nom
    run(app, scenario)


def test_suggestions_empty_state():
    app, _ = make()

    async def scenario(c):
        await login(c)
        body = (await (await c.get("/suggestions")).text()).split('id="suggestions"')[1]
        assert "Aucune suggestion en attente." in body and "data-cb" not in body
    run(app, scenario)


def test_new_pages_with_data_have_no_inline_script_or_safe_style():
    app, db = make()
    db.add_event_if_new(Event(title="RDV", start=NOW + timedelta(days=1), end=NOW + timedelta(days=1, hours=1)))
    db.upsert_task(Task(title="T"))
    p = Project(name="P", last_activity=NOW)
    db.save_project(p)
    db.save_suggestions([Suggestion(batch="b", project_id=p.id, title="S", created_at=NOW)])

    async def scenario(c):
        await login(c)
        for url in ("/a-trier", "/suggestions"):
            text = await (await c.get(url)).text()
            assert not re.search(r"<[a-z][^>]*\son[a-z]+\s*=", text, re.I), url
            assert re.findall(r"<script[^>]*>", text) == ['<script src="/static/app.js" defer>'], url
            assert all(re.fullmatch(r"--c: #[0-9A-Fa-f]{6}", s) for s in re.findall(r'style="([^"]*)"', text.split("<main")[1])), url
            assert "http://" not in text and "https://" not in text, url
    run(app, scenario)
