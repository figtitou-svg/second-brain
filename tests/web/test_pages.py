# tests/web/test_pages.py
from __future__ import annotations

import re
from datetime import timedelta

from bot.fakes import FakeCoach, FakeMemory, FakeRouter
from bot.handlers import BotService, Reply
from coach.calendar_client import FakeCalendarClient
from coach.db import Database
from coach.models import Milestone, Project, Suggestion, Task
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


# --- Task 5 : rendu de l'Agenda et du Récap semaine ------------------------------------------------------

def test_agenda_day_shows_timeline_dues_and_now_line_only_today():
    app, db = make()
    db.upsert_task(Task(title="[BD] TP", due=dt(2026, 10, 27, 18)))

    async def scenario(c):
        await login(c)
        today = await (await c.get("/agenda?jour=2026-10-27")).text()
        assert "[BD] TP" in today and "nowline" in today and "Aujourd" in today
        assert "mardi 27 octobre" in today and "Échéances du jour" in today and "18h00" in today
        other = await (await c.get("/agenda?jour=2026-10-28")).text()
        assert "nowline" not in other and "Rien de prévu" in other and "mercredi 28 octobre" in other
    run(app, scenario)


def test_week_summary_page_bars_and_empty_state():
    from coach.models import Block
    app, db = make()

    async def scenario(c):
        await login(c)
        assert "Aucun bloc terminé" in await (await c.get("/semaine")).text()
    run(app, scenario)

    app, db = make()                                   # une app aiohttp ne se relance pas sur un second serveur
    p = Project(name="Projet IA")
    db.save_project(p)
    t = Task(title="[IA] A", project_id=p.id, status="done")
    db.upsert_task(t)
    db.add_block(Block(task_id=t.id, start=NOW - timedelta(days=1, hours=2), end=NOW - timedelta(days=1, hours=1),
                       outcome="done"))
    db.upsert_task(Task(title="Rendu en retard", due=NOW - timedelta(days=1)))
    db.upsert_task(Task(title="Rendu bientôt", due=NOW + timedelta(days=2)))

    async def scenario2(c):
        await login(c)
        text = await (await c.get("/semaine")).text()
        assert "Projet IA" in text and "1 h" in text and data.project_colors(db)[p.id] in text
        assert "Aucun bloc terminé" not in text and 'style="width: 100%"' in text
        assert "1 tâche terminée sur 1 travaillée" in text
        late = text.split('class="duelist late"')[1]
        assert "Rendu en retard" in late and "Rendu bientôt" not in late.split("</ul>")[0]
        assert "Rendu bientôt" in text
    run(app, scenario2)


def test_agenda_day_positions_overlapping_items_in_lanes_and_escapes_titles():
    from coach.models import Block
    app, db = make()
    a = Task(title="<b>Projet</b> A", status="scheduled")
    b = Task(title="Projet B", status="scheduled")
    db.upsert_task(a)
    db.upsert_task(b)
    db.add_block(Block(task_id=a.id, start=dt(2026, 10, 27, 14), end=dt(2026, 10, 27, 15)))
    db.add_block(Block(task_id=b.id, start=dt(2026, 10, 27, 14, 30), end=dt(2026, 10, 27, 16)))

    async def scenario(c):
        await login(c)
        text = await (await c.get("/agenda?jour=2026-10-27")).text()
        assert "<b>Projet</b>" not in text and "&lt;b&gt;Projet&lt;/b&gt; A" in text
        assert "--lane: 0; --lanes: 2" in text and "--lane: 1; --lanes: 2" in text
        assert "top: 42.86%" in text                    # 14h dans la fenêtre 8h–22h
        assert "14h00–15h00" in text and 'class="hour"' in text
    run(app, scenario)


def test_agenda_week_highlights_today_and_lists_items_as_chips():
    from coach.models import Block
    app, db = make()
    t = Task(title="[IA] Entraîner", status="scheduled")
    db.upsert_task(t)
    db.add_block(Block(task_id=t.id, start=dt(2026, 10, 29, 9), end=dt(2026, 10, 29, 10)))

    async def scenario(c):
        await login(c)
        week = await (await c.get("/agenda?vue=semaine&jour=2026-10-27")).text()
        assert week.count('class="wday') == 7 and week.count('class="wday today"') == 1
        assert 'href="/agenda?jour=2026-10-27" aria-current="date"' in week
        assert 'href="/agenda?vue=semaine&amp;jour=2026-10-19"' in week
        assert 'href="/agenda?vue=semaine&amp;jour=2026-11-02"' in week
        assert "[IA] Entraîner" in week and "09h00" in week and "nowline" not in week
        other = await (await c.get("/agenda?vue=semaine&jour=2026-11-04")).text()
        assert 'class="wday today"' not in other
    run(app, scenario)


# --- Task 6 : rendu du Focus et des Tâches ----------------------------------------------------------------

def test_focus_and_tasks_empty_states():
    app, _ = make()

    async def scenario(c):
        await login(c)
        focus = await (await c.get("/focus")).text()
        assert "Aucune tâche à classer" in focus and "rowcard" not in focus
        taches = await (await c.get("/taches")).text()
        assert "Aucune tâche ouverte" in taches and 'href="/taches?filtre=sans-projet"' in taches
    run(app, scenario)


def test_focus_cards_highlight_top_three_with_bar_reason_and_go_button():
    app, db = make()
    tasks = [Task(title=f"Tâche {i}", due=NOW + timedelta(days=i + 1)) for i in range(4)]
    for t in tasks:
        db.upsert_task(t)
    db.upsert_task(Task(title="<b>x</b> l'essai", due=NOW + timedelta(days=9)))

    async def scenario(c):
        await login(c)
        text = await (await c.get("/focus")).text()
        assert text.count('class="rowcard focuscard top"') == 3
        assert text.count('class="rowcard focuscard"') == 2
        first = text.split('class="rowcard focuscard')[1]
        assert "Tâche 0" in first and 'class="gbar"' in first and "width: 100%" in first
        assert f'data-cb="nudgego:{tasks[0].id}"' in first and "▶ Je m'y mets</button>" in first
        assert 'class="reason"' in first and "Focus" in first and "⏱" in first
        assert "<b>x</b>" not in text and "&lt;b&gt;x&lt;/b&gt; l&#39;essai" in text
        assert "&amp;#39;" not in text and "&amp;lt;" not in text           # pas de double échappement
    run(app, scenario)


def test_tasks_page_marks_overdue_rows_active_chip_and_escapes_titles():
    app, db = make()
    p = Project(name="Projet IA", last_activity=NOW)
    db.save_project(p)
    m = Milestone(project_id=p.id, title="Modèle", position=0)
    db.save_milestone(m)
    late = Task(title="Rendu en retard", due=NOW - timedelta(days=1), project_id=p.id, milestone_id=m.id)
    soon = Task(title="<b>x</b>", due=NOW + timedelta(days=2), status="scheduled")
    db.upsert_task(late)
    db.upsert_task(soon)

    async def scenario(c):
        await login(c)
        text = await (await c.get("/taches")).text()
        rows = text.split('<li class="rowcard taskcard')[1:]
        late_row = next(r for r in rows if "Rendu en retard" in r)
        soon_row = next(r for r in rows if "&lt;b&gt;x&lt;/b&gt;" in r)
        assert late_row.startswith(' overdue"') and "En retard" in late_row and "Étape : Modèle" in late_row
        assert "overdue" not in soon_row.split(">")[0] and "📌" in soon_row
        assert "<b>x</b>" not in text
        assert data.project_colors(db)[p.id] in text and "Projet IA" in text and "Sans projet" in text
        assert text.count('aria-current="true"') == 1
        assert 'href="/taches?filtre=tout" aria-current="true"' in text
        assert re.findall(r'href="([^"]+)"[^>]*aria-current="page"', text) == ["/taches", "/taches"]
        retard = await (await c.get("/taches?filtre=retard")).text()
        assert 'href="/taches?filtre=retard" aria-current="true"' in retard and retard.count('aria-current="true"') == 1
        assert "Rendu en retard" in retard and "&lt;b&gt;x&lt;/b&gt;" not in retard
        assert re.search(r'filtre=retard"[^>]*>En retard <span class="n">1</span>', retard)
        empty = await (await c.get("/taches?filtre=sans-projet")).text()
        assert "&lt;b&gt;x&lt;/b&gt;" in empty                 # la tâche sans projet
        frag = await (await c.get("/fragment/taches?filtre=semaine")).text()
        assert 'id="nav"' not in frag and 'href="/taches?filtre=semaine" aria-current="true"' in frag
    run(app, scenario)


# --- Task 7 : projets, détail, JavaScript -------------------------------------------------------------------


def test_projects_list_empty_state_and_new_project_button():
    app, _ = make()

    async def scenario(c):
        await login(c)
        text = await (await c.get("/projets")).text()
        assert "Aucun projet. Crée-le avec « Nouveau projet »." in text
        assert re.search(r'<button[^>]*data-say="/projet"[^>]*data-goto="/discussion"[^>]*>[^<]*Nouveau projet', text)
        assert 'class="pcard' not in text
    run(app, scenario)


def test_project_cards_ring_without_arc_at_zero_paused_below_and_alert():
    app, db = make()
    fresh = Project(name="<b>Neuf</b>", last_activity=NOW)
    stalled = Project(name="Stagne", last_activity=NOW - timedelta(days=12))
    paused = Project(name="Vieux", status="paused", last_activity=NOW)
    for p in (fresh, stalled, paused):
        db.save_project(p)
    m = Milestone(project_id=stalled.id, title="Données", position=0, status="done")
    db.save_milestone(m)
    db.save_milestone(Milestone(project_id=stalled.id, title="Modèle", position=1))
    db.upsert_task(Task(title="Nettoyer", project_id=stalled.id))

    async def scenario(c):
        await login(c)
        text = await (await c.get("/projets")).text()
        cards = text.split('<li class="pcard')[1:]
        assert len(cards) == 3
        neuf = next(x for x in cards if "&lt;b&gt;Neuf&lt;/b&gt;" in x)
        stag = next(x for x in cards if "Stagne" in x)
        vieux = next(x for x in cards if "Vieux" in x)
        assert 'class="arc"' not in neuf and "0%" in neuf and f'href="/projets/{fresh.id}"' in neuf
        assert 'class="arc"' in stag and "50%" in stag and "Étape :</span> Modèle" in stag
        assert "Nettoyer" in stag and "1 tâche ouverte" in stag
        assert stag.startswith(' has-alert"') and 'class="alert"' in stag and "12" in stag.split("alert-text")[-1]
        assert 'class="alert"' not in neuf and "sans échéance" in neuf
        assert vieux.startswith(' off"') and "En pause" in vieux
        assert text.index("Vieux") > text.index('id="paused-h"') > text.index("Stagne")
        assert "<b>Neuf</b>" not in text
        frag = await (await c.get("/fragment/projets")).text()
        assert 'id="nav"' not in frag and frag.count('<li class="pcard') == 3
    run(app, scenario)


def _detail_project(db, **kw):
    p = Project(name="Projet IA", last_activity=NOW, deadline=(NOW + timedelta(days=20)).date(),
                profile={"goal": "Entraîner <un> modèle"}, **kw)
    db.save_project(p)
    ms = [Milestone(project_id=p.id, title=t, position=i, status=s)
          for i, (t, s) in enumerate([("Données", "done"), ("Modèle", "open"), ("Rapport", "open")])]
    for m in ms:
        db.save_milestone(m)
    db.upsert_task(Task(title="[IA] Entraîner", project_id=p.id, milestone_id=ms[1].id))
    db.upsert_task(Task(title="[IA] Vrac", project_id=p.id))
    db.save_suggestions([Suggestion(batch="b1", project_id=p.id, title="Idée <i>neuve</i>", created_at=NOW)])
    return p, ms, db.list_batch("b1")[0]


def test_project_detail_header_facts_timeline_loose_suggestions_and_actions():
    app, db = make()
    p, ms, s = _detail_project(db)

    async def scenario(c):
        await login(c)
        text = await (await c.get(f"/projets/{p.id}")).text()
        assert '<a href="/projets">← Projets</a>' in text and "33 %" in text and "J-20" in text
        assert "Objectif" in text and "Entraîner &lt;un&gt; modèle" in text
        steps = text.split('<li class="step ')[1:]
        assert [x.split('"')[0] for x in steps] == ["done", "current", "todo"]
        assert "✅" in steps[0] and "⏳" in steps[1] and 'aria-current="step"' in steps[1]
        assert "[IA] Entraîner" in steps[1]
        tid = next(t.id for t in db.list_project_tasks(p.id) if t.title == "[IA] Entraîner")
        for cb in ("td", "tpo", "tnw"):
            assert f'data-cb="{cb}:{tid}"' in steps[1], cb
        assert 'aria-label="Semaine pro. : [IA] Entraîner"' in text
        loose = text.split('id="loose-h"')[1]
        assert "Sans étape" in text and "[IA] Vrac" in loose
        for cb in ("add", "plan", "no"):
            assert f'data-cb="ps:{cb}:{s.id}"' in text, cb
        assert "Idée &lt;i&gt;neuve&lt;/i&gt;" in text and "<i>neuve" not in text
        assert re.search(rf'data-cb="pp:fix:{p.id}" data-discuss="1" data-label="✏️ Corriger"', text)
        assert not re.search(rf'data-cb="pp:pause:{p.id}" data-discuss', text)
    run(app, scenario)


def test_paused_project_hides_suggestions():
    app, db = make()
    p, _, s = _detail_project(db, status="paused")

    async def scenario(c):
        await login(c)
        text = await (await c.get(f"/projets/{p.id}")).text()
        assert "ps:add:" not in text and "Suggestions en attente" not in text
        assert f'data-cb="pp:resume:{p.id}"' in text and "En pause" in text
    run(app, scenario)


def test_tasks_groups_have_stable_keys_for_fold_state():
    app, db = make()
    p = Project(name="Projet IA", last_activity=NOW)
    db.save_project(p)
    db.upsert_task(Task(title="Avec projet", project_id=p.id))
    db.upsert_task(Task(title="Sans", due=NOW + timedelta(days=1)))

    async def scenario(c):
        await login(c)
        text = await (await c.get("/taches")).text()
        assert f'data-gkey="{p.id}"' in text and 'data-gkey="sans-projet"' in text
        assert 'aria-label="Semaine pro. : Avec projet">📅 Semaine pro.</button>' in text
    run(app, scenario)


def test_no_inline_handlers_or_scripts_in_rendered_pages():
    app, db = make()
    p, _, _ = _detail_project(db)
    db.upsert_task(Task(title="Tâche", due=NOW + timedelta(hours=2)))

    async def scenario(c):
        await login(c)
        for url in ["/", "/discussion"] + PAGES + [f"/projets/{p.id}", "/agenda?vue=semaine"]:
            text = await (await c.get(url)).text()
            assert "onclick=" not in text.lower(), url
            assert not re.search(r"<[a-z][^>]*\son[a-z]+\s*=", text, re.I), url
            assert re.findall(r"<script[^>]*>", text) == ['<script src="/static/app.js" defer>'], url
    run(app, scenario)


def test_click_discuss_label_is_capped():
    svc = BotService(FakeCoach([]), FakeMemory(), FakeRouter("x"), 42)
    svc.on_callback = lambda cb: Reply("Réponse")
    app, _ = make(svc=svc)

    async def scenario(c):
        await login(c)
        token = await csrf_of(c)
        r = await c.post("/click", data={"data": "pm:add:x", "discuss": "1", "label": "A" * 500 + "Z"},
                         headers={"X-CSRF": token})
        assert r.status == 200
        page = await (await c.get("/discussion")).text()
        assert "A" * 200 in page and "A" * 201 not in page and "AZ" not in page
    run(app, scenario)


def test_failing_say_returns_error_fragment_and_is_kept_in_discussion():
    svc = BotService(FakeCoach([]), FakeMemory(), FakeRouter("x"), 42)

    def boom(text, source=None):
        raise RuntimeError("bot en panne")
    svc.on_text = boom
    app, _ = make(svc=svc)

    async def scenario(c):
        await login(c)
        token = await csrf_of(c)
        r = await c.post("/say", data={"text": "Lancer un projet robot"}, headers={"X-CSRF": token})
        text = await r.text()
        assert r.status == 200 and 'role="alert"' in text and "Ça n" in text and "bot en panne" not in text
        page = await (await c.get("/discussion")).text()
        said = page.index("Lancer un projet robot")
        assert "Ça n" in page[said:] and "bot en panne" not in page
    run(app, scenario)


# --- Task 8 : corrections visuelles ----------------------------------------------------------------------


def test_cache_control_static_revalidates_and_pages_are_not_stored():
    app, _ = make()

    async def scenario(c):
        await login(c)
        for url in ("/static/app.css", "/static/app.js", "/static/fonts/atkinson-400.woff2"):
            r = await c.get(url)
            assert r.status == 200 and r.headers.get("Cache-Control") == "no-cache", url
            assert r.headers.get("ETag") or r.headers.get("Last-Modified"), url
            assert r.headers.get("X-Content-Type-Options") == "nosniff", url
        for url in ("/", "/taches", "/fragment/taches", "/projets", "/fragment/projets"):
            r = await c.get(url)
            assert r.status == 200 and r.headers.get("Cache-Control") == "no-store", url
            assert "default-src 'self'" in r.headers.get("Content-Security-Policy", ""), url
            assert r.headers.get("X-Content-Type-Options") == "nosniff", url
            assert r.headers.get("Referrer-Policy") == "no-referrer", url
    run(app, scenario)


def test_task_rows_fold_secondary_actions_into_more():
    app, db = make()
    t = Task(title="Envoyer le devis", due=NOW + timedelta(days=2))
    db.upsert_task(t)

    async def scenario(c):
        await login(c)
        text = await (await c.get("/taches")).text()
        row = text.split('<li class="rowcard taskcard')[1].split("</li>")[0]
        visible, more = row.split('<details class="more"')
        assert f'data-cb="td:{t.id}"' in visible and f'data-cb="plan:{t.id}"' in visible
        assert f'data-gkey="t:{t.id}"' in more and "<summary>Plus" in more
        for cb in ("dur", "tpo", "tnw", "ign"):
            assert f'data-cb="{cb}:{t.id}"' in more and f'data-cb="{cb}:' not in visible, cb
        assert 'aria-label="Semaine pro. : Envoyer le devis">📅 Semaine pro.</button>' in more
        assert "<details class=\"more\" open" not in text                        # replié par défaut
    run(app, scenario)


def test_agenda_week_merges_deadlines_with_items_by_time():
    from coach.models import Block
    app, db = make()
    t = Task(title="Bloc du matin", status="scheduled")
    db.upsert_task(t)
    db.add_block(Block(task_id=t.id, start=dt(2026, 10, 29, 9), end=dt(2026, 10, 29, 10)))
    db.upsert_task(Task(title="Rendu tôt", due=dt(2026, 10, 29, 8)))
    db.upsert_task(Task(title="Rendu tard", due=dt(2026, 10, 29, 18)))

    async def scenario(c):
        await login(c)
        week = await (await c.get("/agenda?vue=semaine&jour=2026-10-27")).text()
        day = next(s for s in week.split('<section class="wday')[1:] if "Bloc du matin" in s)
        assert day.index("Rendu tôt") < day.index("Bloc du matin") < day.index("Rendu tard")
        assert "⚑ 08h00" in day and "09h00" in day and "⚑ 18h00" in day
    run(app, scenario)


def test_week_summary_due_times_use_scheduler_timezone():
    from datetime import datetime, timezone
    app, db = make()
    db.upsert_task(Task(title="Rendu UTC", due=datetime(2026, 10, 28, 23, 30, tzinfo=timezone.utc)))

    async def scenario(c):
        await login(c)
        text = await (await c.get("/semaine")).text()
        line = text.split("Rendu UTC")[1].split("</li>")[0]
        assert "jeu. 29 oct." in line and "00h30" in line and "23h30" not in line
    run(app, scenario)


def test_paused_project_card_names_are_h3_under_paused_heading():
    app, db = make()
    db.save_project(Project(name="Actif", last_activity=NOW))
    db.save_project(Project(name="Vieux", status="paused", last_activity=NOW))

    async def scenario(c):
        await login(c)
        text = await (await c.get("/projets")).text()
        assert re.search(r'<h2 class="pc-name">Actif', text)
        assert re.search(r'<h3 class="pc-name">Vieux', text) and '<h2 class="pc-name">Vieux' not in text
    run(app, scenario)


def test_refreshed_fragments_keep_the_focus_anchors_of_the_page():
    """app.js retrouve le focus après un rechargement forcé par data-cb (dans la section id=…), rouvre le
    <details data-gkey> qui l'entoure, et retombe sur le h1 de la page : ces repères doivent être stables."""
    app, db = make()
    p, _, _ = _detail_project(db)
    db.upsert_task(Task(title="Sans projet", due=NOW + timedelta(days=1)))

    async def scenario(c):
        await login(c)
        for url, sid in (("/taches", "taches"), (f"/projets/{p.id}", "projet"), ("/focus", "focus")):
            page = await (await c.get(url)).text()
            frag = await (await c.get(f"/fragment{url}")).text()
            assert re.search(rf'<section id="{sid}" data-refresh="/fragment{url}', page), url
            assert re.search(r"<main[^>]*>\s*(<p[^>]*>.*?</p>\s*)?<h1>", page, re.S), url
            cbs = re.findall(r'data-cb="([^"]+)"', frag)
            section = page.split(f'<section id="{sid}"')[1]
            assert cbs and cbs == re.findall(r'data-cb="([^"]+)"', section), url    # mêmes boutons, même ordre
            assert len(set(cbs)) == len(cbs), url                       # chaque data-cb désigne un seul bouton
            assert re.findall(r'data-gkey="([^"]+)"', frag) == re.findall(r'data-gkey="([^"]+)"', section), url
        taches = await (await c.get("/fragment/taches")).text()
        keys = re.findall(r'data-gkey="([^"]+)"', taches)
        assert "sans-projet" in keys and p.id in keys and sum(k.startswith("t:") for k in keys) == 3
    run(app, scenario)
