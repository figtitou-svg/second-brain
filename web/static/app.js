// web/static/app.js — clics, saisie, rafraîchissement ; aucune dépendance
(() => {
  const csrf = document.querySelector('meta[name="csrf"]')?.content || "";
  const toast = document.getElementById("toast");
  const offline = document.getElementById("offline");

  async function post(url, fields) {
    const body = new URLSearchParams(fields);
    try {
      const r = await fetch(url, {method: "POST", body, headers: {"X-CSRF": csrf}, credentials: "same-origin"});
      offline.hidden = true;
      if (r.status === 401) { location.href = "/login"; return null; }
      if (!r.ok) { show("Action refusée : recharge la page.", true); return null; }
      return await r.text();
    } catch (e) { offline.hidden = false; return null; }
  }

  // html vient du serveur (gabarits échappés, HTML Telegram filtré) ; text=true pour un message local.
  // Une réponse qui porte des boutons ([data-cb], ex. les choix de durée) reste affichée jusqu'au clic ou à ✕.
  // Focus : un bandeau à boutons reçoit le focus sur son premier choix ; à la fermeture (✕, Échap) le focus
  // revient à l'élément qui l'avait ouvert (ou à son remplaçant). origin : l'élément déclencheur, KEEP pour
  // garder celui déjà retenu (choix fait dans le bandeau), absent : l'élément qui a le focus.
  const KEEP = {};
  function show(content, text, origin) {
    if (!content || !content.trim()) return;   // réponse vide : le bandeau n'est pas touché
    const inToast = !toast.hidden && toast.contains(document.activeElement);
    if (origin !== KEEP && !(origin === undefined && inToast)) rememberOrigin(origin || document.activeElement);
    if (text) toast.textContent = content; else toast.innerHTML = content;
    const close = document.createElement("button");
    close.type = "button";
    close.className = "x";
    close.setAttribute("aria-label", "Fermer");
    close.textContent = "✕";
    toast.prepend(close);
    toast.hidden = false;
    clearTimeout(show.t);
    const first = toast.querySelector("[data-cb]");
    if (first) { first.focus(); return; }
    show.t = setTimeout(() => hideToast(false), 6000);
  }

  function rememberOrigin(el) {
    const ok = el && el !== document.body && el.isConnected && !toast.contains(el);
    show.origin = ok ? el : null;
    show.anchor = ok ? describeAnchor(el) : null;
  }

  // --- focus après un rechargement de section -------------------------------------------------------------
  // Un rechargement forcé remplace les boutons : on décrit l'élément (data-cb, puis les data-cb qui le suivent
  // dans sa section) AVANT, et on retrouve APRÈS le même bouton, sinon le suivant encore présent, sinon le titre.

  // pure : bouton à cibler (indice dans buttons, ou -1).
  // anchor = {cb, next: [cb…]} ; buttons = [{cb, reachable}] dans l'ordre du document
  function pickFocus(anchor, buttons) {
    if (!anchor) return -1;
    const same = buttons.findIndex((b) => b.cb === anchor.cb);
    if (same >= 0) return same;                       // même bouton : ses <details> seront rouverts
    for (const cb of anchor.next) {
      const i = buttons.findIndex((b) => b.cb === cb && b.reachable);
      if (i >= 0) return i;
    }
    return -1;
  }

  function describeAnchor(el) {
    const cb = el?.dataset?.cb;
    if (!cb) return null;
    const scope = el.closest("[data-refresh]") || document;
    const all = [...scope.querySelectorAll("[data-cb]")];
    return {cb, next: all.slice(all.indexOf(el) + 1).map((b) => b.dataset.cb), section: scope.id || null};
  }

  // atteignable : aucun <details> fermé autour
  function reachable(el) {
    for (let d = el.parentElement?.closest("details"); d; d = d.parentElement?.closest("details")) {
      if (!d.open) return false;
    }
    return true;
  }

  function focusAnchor(anchor, original) {
    if (original && original.isConnected && !original.disabled) { original.focus(); return; }
    if (!anchor) return;
    const scope = (anchor.section && document.getElementById(anchor.section)) || document;
    const els = [...scope.querySelectorAll("[data-cb]")].filter((b) => !toast.contains(b));
    const i = pickFocus(anchor, els.map((b) => ({cb: b.dataset.cb, reachable: reachable(b)})));
    if (i >= 0) {
      const el = els[i];
      for (let d = el.parentElement?.closest("details"); d; d = d.parentElement?.closest("details")) d.open = true;
      el.focus();
      return;
    }
    const h = document.querySelector("main h1");    // plus rien après : le titre de la page
    if (h) { h.setAttribute("tabindex", "-1"); h.focus(); }
  }

  function focusLost() {
    const a = document.activeElement;
    return !a || a === document.body || !a.isConnected;
  }

  function restoreFocus() { focusAnchor(show.anchor, show.origin); }

  function hideToast(restore) {
    clearTimeout(show.t);
    const had = toast.contains(document.activeElement);
    toast.hidden = true;
    if (restore || had) restoreFocus();
  }

  // une section n'est pas remplacée pendant qu'on s'en sert : focus clavier dedans ou clic en attente ;
  // force (après une action de l'utilisateur) ignore seulement le focus, resté sur le bouton cliqué
  function inUse(el, force) {
    return (!force && el.contains(document.activeElement)) || el.querySelector("[data-cb]:disabled") !== null;
  }

  // les <details data-gkey> (groupes de projet, « Plus » d'une tâche) gardent leur état ouvert ou replié
  // d'un rechargement à l'autre ; ceux qui apparaissent prennent l'état écrit dans le gabarit
  function replaceKeepingFolds(el, html) {
    const state = new Map();
    for (const d of el.querySelectorAll("details[data-gkey]")) state.set(d.dataset.gkey, d.open);
    el.innerHTML = html;
    if (!state.size) return;
    for (const d of el.querySelectorAll("details[data-gkey]")) {
      if (state.has(d.dataset.gkey)) d.open = state.get(d.dataset.gkey);
    }
  }

  // réponse d'erreur du serveur (fragment _error.html : <p class="error" role="alert">)
  function failed(html) {
    const tpl = document.createElement("template");
    tpl.innerHTML = html;
    return tpl.content.querySelector('[role="alert"], .error') !== null;
  }

  // adresse interne seulement (« /chemin », jamais « //hôte » ni « https:… »)
  function internal(url) {
    return typeof url === "string" && url.startsWith("/") && !url.startsWith("//") && !url.startsWith("/\\");
  }

  // jamais deux rechargements en même temps : un appel pendant un rechargement reçoit la même promesse, et un
  // appel forcé ajoute un passage ; la promesse n'est tenue qu'une fois tous les passages finis (DOM à jour)
  function refresh(force) {
    if (refresh.running) { if (force) refresh.again = true; return refresh.running; }
    refresh.running = (async () => {
      try {
        await refreshOnce(force);
        while (refresh.again) { refresh.again = false; await refreshOnce(true); }
      } finally { refresh.running = null; refresh.again = false; }
    })();
    return refresh.running;
  }

  async function refreshOnce(force) {
    for (const el of document.querySelectorAll("[data-refresh]")) {
      if (inUse(el, force)) continue;
      const ctrl = new AbortController();
      const timer = setTimeout(() => ctrl.abort(), 10000);
      try {
        const r = await fetch(el.dataset.refresh, {credentials: "same-origin", signal: ctrl.signal});
        if (r.redirected || r.status === 401) { location.href = "/login"; return; }
        if (r.ok) {
          const html = await r.text();
          if (!inUse(el, force)) replaceKeepingFolds(el, html);
          offline.hidden = true;
        }
      } catch (e) { offline.hidden = false; }
      finally { clearTimeout(timer); }
    }
  }

  async function say(text, target) {
    const html = await post("/say", {text});
    if (html === null) return;
    const thread = target && document.querySelector(target);
    if (thread) { thread.insertAdjacentHTML("beforeend", html); thread.lastElementChild?.scrollIntoView({block: "end"}); }
    else { show(html); refresh(true); }
  }

  document.addEventListener("click", async (ev) => {
    if (ev.target.closest("#toast > .x")) { hideToast(true); return; }
    const cb = ev.target.closest("[data-cb]");
    const sy = ev.target.closest("[data-say]");
    if (cb) {
      ev.preventDefault();
      if (cb.disabled) return;
      const inToast = toast.contains(cb);
      // décrit avant le clic : une fois désactivé le bouton peut perdre le focus, puis disparaître au rechargement
      const hadFocus = document.activeElement === cb || inToast;
      const anchor = inToast ? show.anchor : describeAnchor(cb);
      const original = inToast ? show.origin : cb;
      cb.disabled = true;
      if (cb.dataset.discuss === "1") {
        // suite de conversation : la réponse est rangée dans la Discussion, on y va
        const label = (cb.dataset.label || cb.textContent || "").trim();
        const html = await post("/click", {data: cb.dataset.cb, discuss: "1", label});
        if (html === null || !html.trim() || failed(html)) { cb.disabled = false; show(html, false, cb); return; }
        location.href = "/discussion";     // le bouton reste désactivé : pas de second envoi avant le départ
        return;
      }
      const html = await post("/click", {data: cb.dataset.cb});
      cb.disabled = false;
      if (html === null) return;
      // dans un fil : la réponse remplace le message qui portait le bouton (comme Telegram l'édite)
      const reply = cb.closest(".exchange .reply");
      if (reply && !reply.closest("#toast")) {
        const tpl = document.createElement("template");
        tpl.innerHTML = html;
        const fresh = tpl.content.querySelectorAll(".reply");
        if (fresh.length) { reply.replaceWith(...fresh); return; }
      }
      show(html, false, inToast ? KEEP : cb);
      await refresh(true);                      // DOM remplacé : le focus se rétablit seulement maintenant
      if (hadFocus && focusLost()) focusAnchor(anchor, original);
    } else if (sy) {
      ev.preventDefault();
      const goto = sy.dataset.goto;
      if (internal(goto) && !document.querySelector("#thread")) {
        // « Nouveau projet » : la commande part, la conversation continue dans la Discussion
        if (sy.disabled) return;
        sy.disabled = true;
        const html = await post("/say", {text: sy.dataset.say});
        if (html === null || !html.trim() || failed(html)) { sy.disabled = false; show(html, false, sy); return; }
        location.href = goto;              // le bouton reste désactivé : pas de second envoi avant le départ
        return;
      }
      say(sy.dataset.say, document.querySelector("#thread") ? "#thread" : null);
    }
  });

  document.addEventListener("submit", async (ev) => {
    const form = ev.target;
    if (form.matches("[data-say-form]")) {
      ev.preventDefault();
      const input = form.querySelector("input[name=text]");
      const text = input.value.trim();
      if (!text) return;
      input.value = "";
      say(text, form.dataset.target || null);
    } else if (form.matches("[data-post]")) {
      ev.preventDefault();
      await post(form.action, {});
      location.href = "/login";
    }
  });

  // menu en tiroir (téléphone)
  const toggle = document.querySelector(".menu-toggle"), nav = document.getElementById("nav"),
        scrim = document.querySelector(".scrim");
  function setMenu(open) {
    if (!nav || !toggle) return;
    const was = nav.classList.contains("open");
    nav.classList.toggle("open", open);
    toggle.setAttribute("aria-expanded", String(open));
    toggle.setAttribute("aria-label", open ? "Fermer le menu" : "Ouvrir le menu");
    if (scrim) scrim.hidden = !open;
    if (open) nav.querySelector("a, button")?.focus();
    else if (was) toggle.focus();
  }
  toggle?.addEventListener("click", () => setMenu(!nav.classList.contains("open")));
  scrim?.addEventListener("click", () => setMenu(false));
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    // le tiroir d'abord (il est au premier plan), le bandeau à l'Échap suivant ; jamais pendant la saisie
    if (nav?.classList.contains("open")) { setMenu(false); return; }
    const typing = e.target.closest?.("input, textarea, select, .composer");
    if (!toast.hidden && !typing) hideToast(true);
  });

  // compte à rebours de l'anneau, chaque minute, sans appel au serveur
  // (nombre, arc et libellé lu par les lecteurs d'écran ; l'arc disparaît à 0, comme côté serveur)
  setInterval(() => {
    for (const t of document.querySelectorAll("[data-countdown]")) {
      const n = parseInt(t.textContent, 10);
      if (!(n > 0)) continue;
      const left = n - 1, svg = t.closest("svg");
      t.textContent = String(left);
      if (!svg) continue;
      svg.setAttribute("aria-label", `${left} minutes ${svg.dataset.what || ""}`.trim());
      const arc = svg.querySelector(".arc"), total = parseInt(svg.dataset.total, 10);
      if (!arc) continue;
      if (left === 0 || !(total > 0)) { if (left === 0) arc.remove(); continue; }
      const offset = 100 - Math.min(left / total, 1) * 100;
      arc.setAttribute("stroke-dashoffset", String(Math.round(offset * 10) / 10));
    }
  }, 60000);

  // rechargement des sections toutes les 60 s, seulement si la page est visible
  setInterval(() => { if (document.visibilityState === "visible") refresh(); }, 60000);
  document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") refresh(); });
})();
