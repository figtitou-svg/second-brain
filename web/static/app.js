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

  // html vient du serveur (gabarits échappés, HTML Telegram filtré) ; text=true pour un message local
  function show(content, text) {
    if (text) toast.textContent = content; else toast.innerHTML = content;
    if (!toast.textContent.trim()) return;
    toast.hidden = false;
    clearTimeout(show.t);
    show.t = setTimeout(() => { toast.hidden = true; }, 6000);
  }

  async function refresh() {
    for (const el of document.querySelectorAll("[data-refresh]")) {
      try {
        const r = await fetch(el.dataset.refresh, {credentials: "same-origin"});
        if (r.redirected || r.status === 401) { location.href = "/login"; return; }
        if (r.ok) { el.innerHTML = await r.text(); offline.hidden = true; }
      } catch (e) { offline.hidden = false; }
    }
  }

  async function say(text, target) {
    const html = await post("/say", {text});
    if (html === null) return;
    const thread = target && document.querySelector(target);
    if (thread) { thread.insertAdjacentHTML("beforeend", html); thread.lastElementChild?.scrollIntoView({block: "end"}); }
    else { show(html); refresh(); }
  }

  document.addEventListener("click", async (ev) => {
    const cb = ev.target.closest("[data-cb]");
    const sy = ev.target.closest("[data-say]");
    if (cb) {
      ev.preventDefault();
      if (cb.disabled) return;
      cb.disabled = true;
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
      refresh();
    } else if (sy) {
      ev.preventDefault();
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
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") setMenu(false); });

  // compte à rebours de l'anneau, chaque minute, sans appel au serveur
  setInterval(() => {
    for (const t of document.querySelectorAll("[data-countdown]")) {
      const n = parseInt(t.textContent, 10);
      if (n > 0) t.textContent = String(n - 1);
    }
  }, 60000);

  // rechargement des sections toutes les 60 s, seulement si la page est visible
  setInterval(() => { if (document.visibilityState === "visible") refresh(); }, 60000);
  document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") refresh(); });
})();
