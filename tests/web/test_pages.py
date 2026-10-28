# tests/web/test_pages.py
from __future__ import annotations

import re
from datetime import timedelta

from bot.fakes import FakeCoach, FakeMemory, FakeRouter
from bot.handlers import BotService, Reply
from coach.calendar_client import FakeCalendarClient
from coach.db import Database
from coach.models import Milestone, Project, Task
from coach.rules import load_rules
from coach.scheduler import CoachScheduler
from tests.coach.conftest import FROZEN, dt
from tests.web.test_server import PASSWORD, csrf_of, login, run
from web import data, server, system

NOW = dt(2026, 10, 27, 10, 27)


def make(views=True, svc=None):
    db = Database(":memory:")
    cal = FakeCalendarClient()
    sched = CoachScheduler(db, cal, load_rules(FROZEN), lambda t, b=None: None, lambda: NOW)
    agenda = data.AgendaCache(cal.agenda)
    if views is True:
        views = data.Views(db, sched, agenda, clock=lambda: NOW)
    deps = server.Deps(
        svc=svc or BotService(FakeCoach([]), FakeMemory(), FakeRouter("x"), 42),
        home=lambda: data.build_home(db, sched, agenda, NOW), machine=system.machine, errors=system.ErrorLog(),
        components=lambda: [], quota=lambda: "q", views=views or None)
    return server.create_app(deps, PASSWORD, "0" * 64, clock=lambda: 1_800_000_000.0), db


PAGES = ["/agenda", "/focus", "/taches", "/projets", "/semaine"]


def test_every_page_and_fragment_requires_login_and_renders_on_empty_db():
    app, _ = make()

    async def scenario(c):
        for url in PAGES + [f"/fragment/{p[1:]}" for p in PAGES] + ["/projets/x", "/fragment/projets/x"]:
            assert (await c.get(url, allow_redirects=False)).status == 302, url
        await login(c)
        for url in PAGES:
            r = await c.get(url)
            text = await r.text()
            assert r.status == 200 and "indisponible" not in text, url
            assert f'data-refresh="/fragment/{url[1:]}' in text, url
            assert "default-src 'self'" in r.headers.get("Content-Security-Policy", ""), url
        for url in PAGES:
            r = await c.get(f"/fragment/{url[1:]}")
            text = await r.text()
            assert r.status == 200 and 'id="nav"' not in text and "indisponible" not in text, url
    run(app, scenario)


def test_menu_has_five_new_links_and_four_remaining_soon_entries():
    app, _ = make()

    async def scenario(c):
        await login(c)
        page = await (await c.get("/agenda")).text()
        for href in ("/agenda", "/focus", "/taches", "/projets", "/semaine"):
            assert f'href="{href}"' in page, href
        assert page.count("bientôt") == 4
        assert 'aria-current="page"' in page
        current = re.findall(r'href="([^"]+)"[^>]*aria-current="page"', page)
        assert current == ["/agenda", "/agenda"]                # menu latéral et barre basse : Agenda seul
        bottom = page.split('class="bottom"')[1]
        assert 'href="/agenda"' in bottom and 'href="/taches"' in bottom and "à venir" not in bottom
    run(app, scenario)


def test_project_subpage_marks_projects_entry_current():
    app, db = make()
    p = Project(name="Projet IA", last_activity=NOW)
    db.save_project(p)

    async def scenario(c):
        await login(c)
        page = await (await c.get(f"/projets/{p.id}")).text()
        assert 'href="/projets" title="Projets" aria-current="page"' in page
    run(app, scenario)


def test_bad_query_params_fall_back():
    app, db = make()

    async def scenario(c):
        await login(c)
        for url in ("/agenda?jour=abc&vue=x", "/agenda?jour=2026-02-30", "/agenda?jour=9999-12-31",
                    "/agenda?jour=20261027", "/fragment/agenda?jour=abc&vue=%22%3E"):
            r = await c.get(url)
            text = await r.text()
            assert r.status == 200 and "indisponible" not in text and "%22" not in text, url
            assert 'href="/agenda?jour=2026-10-26"' in text, url                   # retombe sur aujourd'hui
            if not url.startswith("/fragment/"):
                assert 'data-refresh="/fragment/agenda?vue=jour&amp;jour=2026-10-27"' in text, url
        r = await c.get("/taches?filtre=%3Cscript%3E")
        text = await r.text()
        assert r.status == 200 and "<script>alert" not in text and "&lt;script&gt;" not in text.split("<main")[1]
        assert 'data-refresh="/fragment/taches?filtre=tout"' in text
        r = await c.get("/projets/inconnu")
        assert r.status == 404 and "Projet introuvable" in await r.text() and "Second Brain" in await r.text()
        assert 'id="nav"' in await r.text()
        r = await c.get("/fragment/projets/inconnu")
        assert r.status == 404 and "Projet introuvable" in await r.text() and 'id="nav"' not in await r.text()
    run(app, scenario)


def test_project_detail_page_renders_actions_with_telegram_callbacks():
    app, db = make()
    p = Project(name="Projet IA", last_activity=NOW)
    db.save_project(p)
    m = Milestone(project_id=p.id, title="Modèle", position=0)
    db.save_milestone(m)
    db.upsert_task(Task(title="[IA] Entraîner", project_id=p.id, milestone_id=m.id))

    async def scenario(c):
        await login(c)
        r = await c.get(f"/projets/{p.id}")
        text = await r.text()
        assert r.status == 200 and "Projet IA" in text and "[IA] Entraîner" in text
        assert f'data-refresh="/fragment/projets/{p.id}"' in text
        assert f'data-cb="pm:ok:{m.id}"' in text and f'data-cb="pp:pause:{p.id}"' in text
        assert f'data-cb="pm:add:{p.id}"' in text and 'data-discuss="1"' in text
        frag = await (await c.get(f"/fragment/projets/{p.id}")).text()
        assert "[IA] Entraîner" in frag and 'id="nav"' not in frag
    run(app, scenario)


def test_tasks_page_buttons_use_existing_callbacks():
    app, db = make()
    t = Task(title="Envoyer le devis", due=NOW + timedelta(days=2))
    db.upsert_task(t)

    async def scenario(c):
        await login(c)
        text = await (await c.get("/taches")).text()
        for cb in (f"td:{t.id}", f"plan:{t.id}", f"dur:{t.id}", f"tpo:{t.id}", f"tnw:{t.id}", f"ign:{t.id}"):
            assert f'data-cb="{cb}"' in text, cb
        assert 'href="/taches?filtre=retard"' in text
        retard = await (await c.get("/taches?filtre=retard")).text()
        assert "Envoyer le devis" not in retard and 'data-refresh="/fragment/taches?filtre=retard"' in retard
    run(app, scenario)


def test_focus_page_has_adjust_buttons_and_reasons():
    app, db = make()
    t = Task(title="Rendre le TP", due=NOW + timedelta(days=1))
    db.upsert_task(t)

    async def scenario(c):
        await login(c)
        text = await (await c.get("/focus")).text()
        assert "Rendre le TP" in text and f'data-cb="fup:{t.id}"' in text and f'data-cb="fdn:{t.id}"' in text
    run(app, scenario)


def test_agenda_page_block_buttons_and_navigation():
    from coach.models import Block
    app, db = make()
    t = Task(title="[IA] Entraîner", status="scheduled")
    db.upsert_task(t)
    b = Block(task_id=t.id, start=dt(2026, 10, 27, 14), end=dt(2026, 10, 27, 15))
    db.add_block(b)

    async def scenario(c):
        await login(c)
        text = await (await c.get("/agenda?jour=2026-10-27")).text()
        assert f'data-cb="ok:{b.id}"' in text and f'data-cb="rep:{b.id}"' in text
        assert 'href="/agenda?jour=2026-10-26"' in text and 'href="/agenda?jour=2026-10-28"' in text
        week = await (await c.get("/agenda?vue=semaine&jour=2026-10-27")).text()
        assert "lun." in week.lower() and "dim." in week.lower()
        assert 'data-refresh="/fragment/agenda?vue=semaine&amp;jour=2026-10-27"' in week
    run(app, scenario)


def test_click_with_discuss_flag_is_kept_in_discussion():
    svc = BotService(FakeCoach([]), FakeMemory(), FakeRouter("x"), 42)
    answers = {"pp:fix:abc": "✏️ Dis-moi ce qu'il faut changer", "td:x": "✅ Tâche faite sans suite"}
    svc.on_callback = lambda cb: Reply(answers[cb])
    app, _ = make(svc=svc)

    async def scenario(c):
        await login(c)
        token = await csrf_of(c)
        r = await c.post("/click", data={"data": "pp:fix:abc", "discuss": "1", "label": "✏️ Corriger"},
                         headers={"X-CSRF": token})
        assert r.status == 200 and "Dis-moi" in await r.text()
        r = await c.post("/click", data={"data": "td:x"}, headers={"X-CSRF": token})
        assert r.status == 200 and "Tâche faite sans suite" in await r.text()
        page = await (await c.get("/discussion")).text()
        assert "Dis-moi ce qu&#39;il faut changer" in page or "Dis-moi ce qu'il faut changer" in page
        assert "✏️ Corriger" in page                              # texte « dit » = libellé du bouton
        assert "Tâche faite sans suite" not in page               # l'échange sans drapeau n'y entre pas
        # sans libellé : la chaîne de rappel sert de texte « dit » ; un drapeau autre que "1" est ignoré
        await c.post("/click", data={"data": "td:x", "discuss": "oui"}, headers={"X-CSRF": token})
        assert "Tâche faite sans suite" not in await (await c.get("/discussion")).text()
        await c.post("/click", data={"data": "td:x", "discuss": "1"}, headers={"X-CSRF": token})
        page = await (await c.get("/discussion")).text()
        assert "Tâche faite sans suite" in page and ">td:x<" in page
    run(app, scenario)


def test_click_with_discuss_flag_keeps_csrf_check():
    calls = []
    svc = BotService(FakeCoach([]), FakeMemory(), FakeRouter("x"), 42)
    svc.on_callback = lambda cb: calls.append(cb) or Reply("ok")
    app, _ = make(svc=svc)

    async def scenario(c):
        await login(c)
        r = await c.post("/click", data={"data": "pp:fix:abc", "discuss": "1", "label": "Libellé-refusé"})
        assert r.status == 403 and calls == []
        assert "Libellé-refusé" not in await (await c.get("/discussion")).text()
    run(app, scenario)


class _FocusBoom(data.Views):
    def focus(self):
        raise RuntimeError("classement illisible")


def test_failing_page_keeps_layout_and_other_pages_work():
    db = Database(":memory:")
    cal = FakeCalendarClient()
    sched = CoachScheduler(db, cal, load_rules(FROZEN), lambda t, b=None: None, lambda: NOW)
    db.upsert_task(Task(title="Envoyer le devis", due=NOW + timedelta(days=2)))
    app, _ = make(views=_FocusBoom(db, sched, data.AgendaCache(cal.agenda), clock=lambda: NOW))

    async def scenario(c):
        await login(c)
        r = await c.get("/focus")
        page = await r.text()
        assert r.status == 200 and "<!doctype html>" in page and 'id="nav"' in page
        assert "indisponible" in page and 'role="alert"' in page
        assert 'href="/focus" title="Focus" aria-current="page"' in page
        r = await c.get("/fragment/focus")
        frag = await r.text()
        assert r.status == 200 and "indisponible" in frag and 'id="nav"' not in frag and "<!doctype" not in frag
        r = await c.get("/taches")
        assert r.status == 200 and "Envoyer le devis" in await r.text()
    run(app, scenario)


def test_pages_without_views_say_unavailable():
    app, _ = make(views=False)

    async def scenario(c):
        await login(c)
        for url in PAGES:
            r = await c.get(url)
            text = await r.text()
            assert r.status == 200 and "indisponible" in text and 'id="nav"' in text, url
            frag = await (await c.get(f"/fragment/{url[1:]}")).text()
            assert "indisponible" in frag and 'id="nav"' not in frag, url
    run(app, scenario)
