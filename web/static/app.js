// web/static/app.js — clics, saisie, rafraîchissement ; aucune dépendance
(() => {
  const csrf = document.querySelector('meta[name="csrf"]')?.content || "";
  const toast = document.getElementById("toast");
  const offline = document.getElementById("offline");

  // Chargement : une barre en haut de page tant qu'une requête ou un changement de page est en cours. Elle
  // n'apparaît qu'après 200 ms (pas de clignotement sur une réponse rapide) ; le rafraîchissement automatique
  // de fond ne l'allume pas, seulement ce que l'utilisateur a déclenché.
  const loading = document.getElementById("loading");
  let pending = 0, loadingTimer = 0;
  function busy(on) {
    pending = Math.max(0, pending + (on ? 1 : -1));
    if (!loading) return;
    if (pending === 0) { clearTimeout(loadingTimer); loadingTimer = 0; loading.hidden = true; }
    else if (!loadingTimer && loading.hidden) {
      loadingTimer = setTimeout(() => { loadingTimer = 0; if (pending > 0) loading.hidden = false; }, 200);
    }
  }
  // bouton en attente de réponse : désactivé, avec une roue (aria-busy) ; le retour arrière du navigateur
  // peut rendre une page figée en cours de chargement : on repart de zéro
  function lock(el, on) {
    el.disabled = on;
    if (on) el.setAttribute("aria-busy", "true"); else el.removeAttribute("aria-busy");
  }
  window.addEventListener("pageshow", (ev) => {
    if (!ev.persisted) return;
    pending = 0; busy(true); busy(false);
    for (const b of document.querySelectorAll("[aria-busy=true]")) lock(b, false);
  });

  let ownAt = 0;                                // dernière action de cette page : son propre « changed » est ignoré
  async function post(url, fields) {
    const body = new URLSearchParams(fields);
    busy(true);
    ownAt = Date.now();
    try {
      const r = await fetch(url, {method: "POST", body, headers: {"X-CSRF": csrf}, credentials: "same-origin"});
      offline.hidden = true;
      if (r.status === 401) { location.href = "/login"; return null; }
      if (!r.ok) { show("Action refusée : recharge la page.", true); return null; }
      return await r.text();
    } catch (e) { offline.hidden = false; return null; }
    finally { busy(false); }
  }

  // html vient du serveur (gabarits échappés, HTML Telegram filtré) ; text=true pour un message local.
  // Une réponse qui porte des boutons ([data-cb], ex. les choix de durée) reste affichée jusqu'au clic ou à ✕.
  // Focus : un bandeau à boutons reçoit le focus sur son premier choix ; à la fermeture (✕, Échap) le focus
  // revient à l'élément qui l'avait ouvert (ou à son remplaçant). origin : l'élément déclencheur, KEEP pour
  // garder celui déjà retenu (choix fait dans le bandeau), absent : l'élément qui a le focus.
  const KEEP = {};
  // quiet (bandeau « direct ») : ne prend jamais le focus ; l'élément qui l'a est seulement retenu, pour que ✕
  // y ramène plutôt qu'à un déclencheur plus ancien.
  function show(content, text, origin, quiet) {
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
    // « Annuler » seul : le bandeau se referme de lui-même, sans prendre le focus (on peut l'ignorer)
    if (first && first.dataset.cb.startsWith("undo:") && toast.querySelectorAll("[data-cb]").length === 1) {
      show.t = setTimeout(() => hideToast(false), 8000);
      return;
    }
    if (first) { if (!quiet) first.focus(); return; }     // bandeau « direct » : jamais de vol de focus (saisie en cours)
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
    return (!force && el.contains(document.activeElement)) || el.querySelector("[data-cb]:disabled, .swiping") !== null;
  }

  // Groupes de projet (details.tgroup) : ouverts ou repliés, le choix est retenu sur l'appareil. On le retrouve en
  // revenant sur la page (après avoir terminé une tâche, par exemple), pas seulement au rechargement de la section.
  const FOLD = "fold:";
  function storedFold(key) {
    try { const v = localStorage.getItem(FOLD + key); return v === null ? null : v === "1"; } catch (e) { return null; }
  }
  function restoreFolds(root) {
    for (const d of root.querySelectorAll("details.tgroup[data-gkey]")) {
      const v = storedFold(d.dataset.gkey);
      if (v !== null && d.open !== v) d.open = v;
    }
  }
  // l'événement toggle ne remonte pas : écoute en capture
  document.addEventListener("toggle", (ev) => {
    const d = ev.target;
    if (!(d instanceof HTMLDetailsElement) || !d.matches("details.tgroup[data-gkey]") || filtering) return;
    try { localStorage.setItem(FOLD + d.dataset.gkey, d.open ? "1" : "0"); } catch (e) { /* retenu pour cette page seulement */ }
  }, true);
  restoreFolds(document);

  // Recherche dans les tâches : filtre les cartes affichées, sans appel au serveur ; les groupes qui contiennent
  // un résultat s'ouvrent (sans que ce soit retenu comme un choix), les autres disparaissent.
  const tfilter = document.getElementById("tfilter");
  const fold = (s) => s.normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase();
  let filtering = false;
  function applyFilter() {
    const zone = document.getElementById("taches");
    if (!tfilter || !zone) return;
    const q = fold(tfilter.value.trim());
    filtering = q !== "";
    zone.classList.toggle("filtering", filtering);
    for (const li of zone.querySelectorAll("li.taskcard")) li.hidden = filtering && !fold(li.textContent).includes(q);
    for (const d of zone.querySelectorAll("details.tgroup")) {
      const hit = d.querySelector("li.taskcard:not([hidden])") !== null;
      d.hidden = filtering && !hit;
      if (filtering && hit) d.open = true;
    }
    if (!filtering) restoreFolds(zone);
  }
  tfilter?.addEventListener("input", applyFilter);

  // les <details data-gkey> (groupes de projet, « Plus » d'une tâche) gardent leur état ouvert ou replié
  // d'un rechargement à l'autre ; ceux qui apparaissent prennent l'état écrit dans le gabarit
  function replaceKeepingFolds(el, html) {
    const state = new Map();
    for (const d of el.querySelectorAll("details[data-gkey]")) state.set(d.dataset.gkey, d.open);
    el.innerHTML = html;
    restoreFolds(el);
    applyFilter();
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
    if (force) busy(true);
    refresh.running = (async () => {
      try {
        await refreshOnce(force);
        while (refresh.again) { refresh.again = false; await refreshOnce(true); }
      } finally { refresh.running = null; refresh.again = false; if (force) busy(false); }
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

  // Son des messages : synthétisé par le navigateur (aucun fichier), deux notes douces à l'arrivée d'une réponse,
  // une seule à l'envoi. Le navigateur n'autorise le son qu'après un geste : le contexte audio est donc créé
  // au clic ou à l'envoi. Le bouton 🔔 le coupe ; le choix est retenu sur l'appareil.
  const soundBtn = document.querySelector("[data-sound]");
  let muted = false, audio = null;
  try { muted = localStorage.getItem("sound") === "off"; } catch (e) { /* stockage indisponible : son activé */ }
  const SEND = [[740, 0, 0.16]], RECEIVE = [[880, 0, 0.3], [1318.5, 0.12, 0.5]];
  function unlockAudio() {                   // sur toutes les pages : le bandeau « direct » sonne aussi hors Discussion
    try {
      audio = audio || new (window.AudioContext || window.webkitAudioContext)();
      if (audio.state === "suspended") audio.resume();
    } catch (e) { audio = null; }
  }
  function chime(notes) {
    if (muted || !audio) return;
    try {
      const t0 = audio.currentTime;
      for (const [freq, at, len] of notes) {
        const osc = audio.createOscillator(), gain = audio.createGain();
        osc.type = "sine"; osc.frequency.value = freq;
        gain.gain.setValueAtTime(0.0001, t0 + at);
        gain.gain.exponentialRampToValueAtTime(0.1, t0 + at + 0.012);
        gain.gain.exponentialRampToValueAtTime(0.0001, t0 + at + len);
        osc.connect(gain); gain.connect(audio.destination);
        osc.start(t0 + at); osc.stop(t0 + at + len + 0.05);
      }
    } catch (e) { /* pas de son plutôt qu'une erreur */ }
  }
  function paintSound() {
    if (!soundBtn) return;
    soundBtn.textContent = muted ? "🔕" : "🔔";
    soundBtn.setAttribute("aria-pressed", String(!muted));
    soundBtn.setAttribute("aria-label", muted ? "Son des messages : coupé" : "Son des messages : activé");
  }
  soundBtn?.addEventListener("click", () => {
    muted = !muted;
    try { localStorage.setItem("sound", muted ? "off" : "on"); } catch (e) { /* retenu pour cette page seulement */ }
    paintSound();
    unlockAudio(); chime(RECEIVE);
  });
  paintSound();
  // premier geste sur n'importe quelle page (toucher, clavier) : le son est autorisé dès lors
  for (const type of ["pointerdown", "keydown"]) document.addEventListener(type, unlockAudio, {capture: true, once: true});

  async function say(text, target) {
    const thread = target && document.querySelector(target);
    // dans la Discussion, comme un SMS : ton message part tout de suite, puis trois points le temps que le bot réponde
    let pending = null, typing = null;
    if (thread) {
      pending = document.createElement("div");
      pending.className = "exchange pending";
      const said = document.createElement("p");
      said.className = "said arrive";
      said.textContent = text;
      typing = document.createElement("div");
      typing.className = "reply typing arrive";
      typing.innerHTML = '<span class="dots" aria-hidden="true"><i></i><i></i><i></i></span>'
        + '<span class="sr-only">Réponse en cours…</span>';
      pending.append(said, typing);
      thread.append(pending);
      pending.scrollIntoView({block: "end"});
      chime(SEND);
    }
    const html = await post("/say", {text});
    if (html === null) {                          // échec : le message n'est pas parti, on le rend à la saisie
      pending?.remove();
      const box = document.getElementById("say");
      if (pending && box && !box.value) box.value = text;
      return;
    }
    if (thread) {
      const tpl = document.createElement("template");
      tpl.innerHTML = html;
      for (const r of tpl.content.querySelectorAll(".reply")) r.classList.add("arrive");
      const nodes = [...tpl.content.children];
      if (nodes.length) { pending.replaceWith(...nodes); nodes.at(-1).scrollIntoView({block: "end"}); chime(RECEIVE); }
      else typing.remove();                       // réponse vide : le message reste, sans les points
    } else { show(html); refresh(true); }
  }

  document.addEventListener("click", async (ev) => {
    unlockAudio();
    const link = ev.target.closest("a[href]");
    if (link && !ev.defaultPrevented && ev.button === 0 && !(ev.metaKey || ev.ctrlKey || ev.shiftKey || ev.altKey)
        && (!link.target || link.target === "_self") && !link.hasAttribute("download")
        && internal(link.getAttribute("href"))) busy(true);      // la page change : la barre reste jusqu'à l'arrivée
    if (ev.target.closest("#toast > .x")) { hideToast(true); return; }
    const cb = ev.target.closest("[data-cb]");
    const sy = ev.target.closest("[data-say]");
    if (cb) {
      ev.preventDefault();
      if (cb.disabled) return;
      if (cb.dataset.confirm && !window.confirm(cb.dataset.confirm)) return;
      const inToast = toast.contains(cb);
      // décrit avant le clic : une fois désactivé le bouton peut perdre le focus, puis disparaître au rechargement
      const hadFocus = document.activeElement === cb || inToast;
      const anchor = inToast ? show.anchor : describeAnchor(cb);
      const original = inToast ? show.origin : cb;
      lock(cb, true);
      if (cb.dataset.discuss === "1") {
        // suite de conversation : la réponse est rangée dans la Discussion, on y va
        const label = (cb.dataset.label || cb.textContent || "").trim();
        const html = await post("/click", {data: cb.dataset.cb, discuss: "1", label});
        if (html === null || !html.trim() || failed(html)) { lock(cb, false); show(html, false, cb); return; }
        location.href = "/discussion";     // le bouton reste désactivé : pas de second envoi avant le départ
        return;
      }
      const html = await post("/click", {data: cb.dataset.cb});
      lock(cb, false);
      if (html === null) return;
      // dans un fil : la réponse remplace le message qui portait le bouton (comme Telegram l'édite)
      const reply = cb.closest(".exchange .reply");
      if (reply && !reply.closest("#toast")) {
        const tpl = document.createElement("template");
        tpl.innerHTML = html;
        const fresh = tpl.content.querySelectorAll(".reply");
        if (fresh.length) {
          for (const r of fresh) r.classList.add("arrive");
          reply.replaceWith(...fresh); chime(RECEIVE); return;
        }
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
        lock(sy, true);
        const html = await post("/say", {text: sy.dataset.say});
        if (html === null || !html.trim() || failed(html)) { lock(sy, false); show(html, false, sy); return; }
        location.href = goto;              // le bouton reste désactivé : pas de second envoi avant le départ
        return;
      }
      say(sy.dataset.say, document.querySelector("#thread") ? "#thread" : null);
    }
  });

  // --- glisser une tâche sur le côté, comme dans une messagerie ------------------------------------------------
  // → à droite : « Fait » ; ← à gauche : « Supprimer » (ignorer). Rien ne se passe tant qu'on n'est pas allé jusqu'au
  // bout (SWIPE_FULL de la largeur) ET qu'on a relâché : le fond coloré s'intensifie, puis « Relâche pour valider »
  // apparaît ; relâcher avant, revenir en arrière ou Échap annule, la carte revient à sa place.
  const SWIPE_FULL = 0.7;
  let swipe = null, swipeClick = 0;
  function swipeReset(s) {
    s.li.removeAttribute("aria-busy");
    s.li.style.transform = "";
    s.li.style.opacity = "";
    s.bg.remove();
    setTimeout(() => { s.li.style.transition = ""; s.li.classList.remove("swipe-live"); }, 200);
  }
  function swipeEnd(commit) {
    const s = swipe;
    if (!s) return;
    swipe = null;
    s.li.classList.remove("swiping");
    const data = s.dx > 0 ? s.li.dataset.swipeDone : s.li.dataset.swipeDrop;
    s.li.style.transition = "transform .18s ease, opacity .18s ease";
    if (!(commit && s.armed) || !data) { swipeReset(s); return; }
    s.li.style.transform = `translateX(${s.dx > 0 ? "" : "-"}110%)`;
    s.li.style.opacity = "0";
    s.li.setAttribute("aria-busy", "true");
    (async () => {
      const html = await post("/click", {data});
      if (html === null) { swipeReset(s); return; }       // échec : la carte revient, rien n'a été enregistré
      show(html, false, undefined, true);
      await refresh(true);
      s.bg.remove();
    })();
  }
  document.addEventListener("pointerdown", (ev) => {
    const li = ev.target.closest?.("[data-swipe]");
    if (!li || swipe || (ev.pointerType === "mouse" && ev.button !== 0) || ev.target.closest("button, input, select, textarea")) return;
    swipe = {li, id: ev.pointerId, x: ev.clientX, y: ev.clientY, dx: 0, on: false, armed: false, bg: null, w: li.offsetWidth};
  });
  document.addEventListener("pointermove", (ev) => {
    const s = swipe;
    if (!s || ev.pointerId !== s.id) return;
    const dx = ev.clientX - s.x, dy = ev.clientY - s.y;
    if (!s.on) {
      if (Math.abs(dy) > 10 && Math.abs(dy) > Math.abs(dx)) { swipe = null; return; }   // c'est un défilement
      if (Math.abs(dx) < 10) return;
      s.on = true;
      try { s.li.setPointerCapture(ev.pointerId); } catch (e) { /* ignoré */ }
      const bg = s.bg = document.createElement("div");
      bg.className = "swipe-bg";
      bg.setAttribute("aria-hidden", "true");
      bg.innerHTML = '<span class="sw-l"></span><span class="sw-r"></span>';
      const host = s.li.parentElement;
      if (getComputedStyle(host).position === "static") host.style.position = "relative";
      Object.assign(bg.style, {top: s.li.offsetTop + "px", left: s.li.offsetLeft + "px", width: s.li.offsetWidth + "px",
                               height: s.li.offsetHeight + "px", borderRadius: getComputedStyle(s.li).borderRadius});
      host.insertBefore(bg, s.li);
      s.li.classList.add("swiping", "swipe-live");
      s.li.style.transition = "none";
      swipeClick = Date.now() + 400;
    }
    s.dx = Math.max(-s.w, Math.min(s.w, dx));
    const p = Math.min(Math.abs(s.dx) / (s.w * SWIPE_FULL), 1);
    s.armed = p >= 1;
    s.li.style.transform = `translateX(${s.dx}px)`;
    const done = s.dx > 0;
    s.bg.className = `swipe-bg ${done ? "swipe-done" : "swipe-drop"}${s.armed ? " armed" : ""}`;
    s.bg.style.setProperty("--p", p.toFixed(3));
    const ds = s.li.dataset;                         // libellés propres à la liste (À trier : garder / jeter)
    s.bg.firstChild.textContent = done ? (s.armed ? ds.swipeYesGo || "✅ Relâche pour terminer" : ds.swipeYes || "✅ Fait") : "";
    s.bg.lastChild.textContent = done ? "" : (s.armed ? ds.swipeNoGo || "Relâche pour supprimer 🗑" : ds.swipeNo || "Supprimer 🗑");
    ev.preventDefault();
  });
  document.addEventListener("pointerup", (ev) => { if (swipe && ev.pointerId === swipe.id) swipeEnd(true); });
  document.addEventListener("pointercancel", (ev) => { if (swipe && ev.pointerId === swipe.id) swipeEnd(false); });
  document.addEventListener("keydown", (ev) => { if (ev.key === "Escape" && swipe?.on) swipeEnd(false); });
  document.addEventListener("click", (ev) => {      // un glissement ne doit pas ouvrir la tâche
    if (Date.now() < swipeClick) { ev.preventDefault(); ev.stopPropagation(); swipeClick = 0; }
  }, true);

  document.addEventListener("submit", async (ev) => {
    const form = ev.target;
    if (form.matches("[data-say-form]")) {
      ev.preventDefault();
      const input = form.querySelector("input[name=text]");
      const text = input.value.trim();
      if (!text) return;
      input.value = "";
      unlockAudio();
      say(text, form.dataset.target || null);
    } else if (!form.matches("[data-post]") && !ev.defaultPrevented) {
      busy(true);                              // formulaire classique (recherche, connexion) : la page va changer
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

  // Direct : le serveur prévient dès que le bot écrit (« bot ») ou qu'une action change les données (« changed »).
  // Le flux ne porte qu'un numéro : le message lui-même est demandé au serveur (même rendu filtré que partout).
  const seen = (id) => document.querySelector(`[data-ref="${id}"]`) !== null;
  // à appeler AVANT l'ajout : on ne descend que si l'on était déjà en bas du fil, ou si ton message est encore
  // en attente de réponse (bulle .pending, la dernière) ; un lecteur remonté dans le fil n'est pas ramené de force,
  // même quand le dernier échange est l'un des tiens (déjà répondu)
  function following(thread) {
    return thread.getBoundingClientRect().bottom - window.innerHeight < 120         // bas du fil visible
      || thread.lastElementChild?.classList.contains("pending") === true;
  }
  // Rattrapage : un onglet caché n'a pas de flux, les messages du bot publiés pendant ce temps manquent au fil.
  // Au retour (ou après une vraie coupure) on relit le fil et on ajoute, dans l'ordre, ceux qui n'y sont pas ;
  // tes propres échanges (sans data-ref) ne sont jamais réinsérés. Un seul rattrapage à la fois ; aucune erreur
  // affichée ; ni le focus ni la saisie en cours ne sont touchés.
  let catching = false;
  async function catchUp() {
    const thread = document.querySelector("#thread");
    if (!thread || catching) return;
    catching = true;
    try {
      const r = await fetch("/fragment/discussion", {credentials: "same-origin"});
      if (!r.ok || r.redirected) return;
      const tpl = document.createElement("template");
      tpl.innerHTML = await r.text();
      const missed = [...tpl.content.querySelectorAll(".exchange[data-ref]")].filter((ex) => !seen(ex.dataset.ref));
      if (!missed.length) return;
      for (const ex of missed) for (const rp of ex.querySelectorAll(".reply")) rp.classList.add("arrive");
      const follow = following(thread);
      thread.append(...missed);
      if (follow) missed.at(-1).scrollIntoView({block: "end"});
      chime(RECEIVE);
    } catch (e) { /* rattrapage manqué : le prochain retour ou rechargement s'en chargera */ }
    finally { catching = false; }
  }
  async function onBot(id) {
    if (!Number.isInteger(id) || seen(id)) return;
    let html;
    try {
      const r = await fetch(`/fragment/message/${id}`, {credentials: "same-origin"});
      if (!r.ok) return;                                  // inconnu (bot redémarré) : on n'affiche rien
      html = await r.text();
    } catch (e) { return; }
    if (seen(id)) return;                                 // arrivé entre-temps par un rechargement : pas de doublon
    const thread = document.querySelector("#thread");
    if (thread) {
      const tpl = document.createElement("template");
      tpl.innerHTML = html;
      for (const r of tpl.content.querySelectorAll(".reply")) r.classList.add("arrive");
      const nodes = [...tpl.content.children];
      const follow = following(thread);
      thread.append(...nodes);
      if (follow) nodes.at(-1)?.scrollIntoView({block: "end"});
    } else if (toast.hidden || !toast.querySelector("[data-cb]")) {
      show(html, false, undefined, true);                // ne remplace jamais un bandeau qui attend un choix
    }
    chime(RECEIVE);
    refresh();
  }

  // un seul flux par page visible ; EventSource se reconnecte seul (le serveur suggère retry: 5000), sans boucle
  // ici. Un onglet caché ferme son flux (le serveur n'en garde que 4 : sinon les onglets s'évinceraient sans fin)
  // et en rouvre un en revenant ; la fermeture voulue n'est pas une coupure (pas de rechargement forcé).
  let es = null;
  function live() {
    if (!csrf || !window.EventSource || es) return;
    const stream = es = new EventSource("/events");
    let down = false;                          // propre à ce flux : seule une vraie erreur le lève
    stream.addEventListener("open", () => { if (down) { down = false; recover(); } });
    stream.addEventListener("error", () => { down = true; });
    stream.addEventListener("changed", () => { if (Date.now() - ownAt > 4000) refresh(); });
    stream.addEventListener("bot", (ev) => {
      let id;
      try { id = JSON.parse(ev.data).id; } catch (e) { return; }   // événement mal formé : ignoré
      onBot(id).catch(() => {});
    });
  }
  // après une vraie coupure : tout est rechargé, le focus reste où il était (ou sur son remplaçant)
  async function recover() {
    const el = document.activeElement, anchor = describeAnchor(el);
    catchUp();                                 // messages du bot publiés pendant la coupure
    await refresh(true);
    if (el && el !== document.body && focusLost()) focusAnchor(anchor, el);
  }
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "hidden") { es?.close(); es = null; }
    else if (!es) { live(); catchUp(); }      // flux rouvert d'abord : rien ne tombe entre la relecture et lui
  });
  if (document.visibilityState !== "hidden") live();
})();
