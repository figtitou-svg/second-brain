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
  function show(content, text) {
    if (!content || !content.trim()) return;   // réponse vide : le bandeau n'est pas touché
    if (text) toast.textContent = content; else toast.innerHTML = content;
    const close = document.createElement("button");
    close.type = "button";
    close.className = "x";
    close.setAttribute("aria-label", "Fermer");
    close.textContent = "✕";
    toast.prepend(close);
    toast.hidden = false;
    clearTimeout(show.t);
    if (!toast.querySelector("[data-cb]")) show.t = setTimeout(hideToast, 6000);
  }

  function hideToast() {
    clearTimeout(show.t);
    toast.hidden = true;
  }

  // une section n'est pas remplacée pendant qu'on s'en sert : focus clavier dedans ou clic en attente ;
  // force (après une action de l'utilisateur) ignore seulement le focus, resté sur le bouton cliqué
  function inUse(el, force) {
    return (!force && el.contains(document.activeElement)) || el.querySelector("[data-cb]:disabled") !== null;
  }

  // les groupes repliables (<details data-gkey>) gardent leur état replié d'un rechargement à l'autre
  function replaceKeepingFolds(el, html) {
    const closed = new Set();
    for (const d of el.querySelectorAll("details[data-gkey]")) if (!d.open) closed.add(d.dataset.gkey);
    el.innerHTML = html;
    if (!closed.size) return;
    for (const d of el.querySelectorAll("details[data-gkey]")) if (closed.has(d.dataset.gkey)) d.open = false;
  }

  // adresse interne seulement (« /chemin », jamais « //hôte » ni « https:… »)
  function internal(url) {
    return typeof url === "string" && url.startsWith("/") && !url.startsWith("//") && !url.startsWith("/\\");
  }

  async function refresh(force) {
    if (refresh.busy) { if (force) refresh.again = true; return; }   // jamais deux rechargements en même temps
    refresh.busy = true;
    try {
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
    } finally {
      refresh.busy = false;
      if (refresh.again) { refresh.again = false; refresh(true); }
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
    if (ev.target.closest("#toast > .x")) { hideToast(); return; }
    const cb = ev.target.closest("[data-cb]");
    const sy = ev.target.closest("[data-say]");
    if (cb) {
      ev.preventDefault();
      if (cb.disabled) return;
      cb.disabled = true;
      if (cb.dataset.discuss === "1") {
        // suite de conversation : la réponse est rangée dans la Discussion, on y va
        const label = (cb.dataset.label || cb.textContent || "").trim();
        const html = await post("/click", {data: cb.dataset.cb, discuss: "1", label});
        cb.disabled = false;
        if (html !== null) location.href = "/discussion";
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
      show(html);
      refresh(true);
    } else if (sy) {
      ev.preventDefault();
      const goto = sy.dataset.goto;
      if (internal(goto) && !document.querySelector("#thread")) {
        // « Nouveau projet » : la commande part, la conversation continue dans la Discussion
        if (sy.disabled) return;
        sy.disabled = true;
        const html = await post("/say", {text: sy.dataset.say});
        sy.disabled = false;
        if (html !== null) location.href = goto;
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
    if (nav?.classList.contains("open")) setMenu(false);
    else if (!toast.hidden) hideToast();
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
