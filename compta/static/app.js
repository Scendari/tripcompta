// Trip Compta — petites interactions sans framework.
(function () {
  // Thème clair / sombre (mémorisé dans le navigateur)
  const root = document.documentElement;
  try { const t = localStorage.getItem("theme"); if (t) root.dataset.theme = t; } catch (e) {}
  document.addEventListener("click", (ev) => {
    const b = ev.target.closest("[data-theme-toggle]");
    if (!b) return;
    const dark = root.dataset.theme === "dark" ||
      (!root.dataset.theme && matchMedia("(prefers-color-scheme: dark)").matches);
    root.dataset.theme = dark ? "light" : "dark";
    try { localStorage.setItem("theme", root.dataset.theme); } catch (e) {}
  });

  // « Agrandir le texte » : trois tailles (normale, grande, très grande), mémorisées comme le thème
  const TEXT_NAMES = ["normale", "grande", "très grande"];
  const showTextSize = () => document.querySelectorAll("[data-text-size]").forEach((b) => {
    const n = Number(root.dataset.text || 0), next = (n + 1) % 3;
    b.setAttribute("aria-label", (next ? "Agrandir le texte" : "Remettre le texte à la taille normale") +
      " (taille actuelle : " + TEXT_NAMES[n] + ")");
    b.title = b.getAttribute("aria-label");
    const lbl = b.querySelector(".lbl");
    if (lbl) lbl.textContent = next ? " Agrandir le texte" : " Texte normal";
  });
  showTextSize();
  document.addEventListener("click", (ev) => {
    if (!ev.target.closest("[data-text-size]")) return;
    const next = (Number(root.dataset.text || 0) + 1) % 3;
    if (next) root.dataset.text = String(next); else delete root.dataset.text;
    try { if (next) localStorage.setItem("texte", String(next)); else localStorage.removeItem("texte"); } catch (e) {}
    showTextSize();
  });

  // Confirmation avant les actions destructrices
  document.addEventListener("submit", (ev) => {
    const msg = ev.target.dataset.confirm;
    if (msg && !confirm(msg)) ev.preventDefault();
  });

  // Aperçu en direct : <form data-preview="/url" data-preview-target="#id">
  function wirePreview(form) {
    const url = form.dataset.preview;
    const target = document.querySelector(form.dataset.previewTarget);
    if (!url || !target) return;
    let timer = null, seq = 0;
    const run = () => {
      const my = ++seq;
      const fd = new FormData(form);
      fd.delete("files");
      fetch(url, { method: "POST", body: fd })
        .then((r) => r.text())
        .then((html) => { if (my === seq) target.innerHTML = html; })
        .catch(() => {});
    };
    const schedule = () => { clearTimeout(timer); timer = setTimeout(run, 250); };
    form.addEventListener("input", schedule);
    form.addEventListener("change", schedule);
    run();
  }
  document.querySelectorAll("form[data-preview]").forEach(wirePreview);

  // Cocher / décocher tout le monde
  document.addEventListener("click", (ev) => {
    const b = ev.target.closest("[data-check]");
    if (!b) return;
    ev.preventDefault();
    const scope = document.querySelector(b.dataset.scope || "body");
    scope.querySelectorAll('input[type=checkbox][data-who]').forEach((c) => {
      const mode = b.dataset.check, comp = c.dataset.companion === "1";
      const members = (b.dataset.members || "").split(",");
      c.checked = mode === "all" || (mode === "divers" && !comp) || (mode === "companions" && comp)
        || (mode === "group" && members.includes(c.dataset.pid));
      c.dispatchEvent(new Event("change", { bubbles: true }));
    });
  });

  // « Pour qui ? » : boutons « Seulement pour » (rôles, familles, groupes), sélection multiple.
  // Les personnes cochées = l'ensemble des boutons allumés. À l'ouverture (et après un ajustement à la main),
  // un bouton s'allume si les personnes cochées sont exactement les siennes (groupe, puis famille, puis rôle).
  document.querySelectorAll("[data-who-chips]").forEach((box) => {
    const form = box.closest("form");
    const who = () => Array.from(form.querySelectorAll("input[data-who]"));
    const btns = Array.from(box.querySelectorAll("[data-only]"));
    const resets = Array.from(box.querySelectorAll("[data-reset]"));
    const hint = box.querySelector("[data-only-hint]");
    const press = (b, on) => { b.setAttribute("aria-pressed", String(on)); b.classList.toggle("on", on); };
    function syncResets() {       // « Tout le monde » / « Personne » allumés quand c'est la situation (et aucun autre bouton)
      const list = who(), n = list.filter((w) => w.checked).length, any = btns.some((b) => b.classList.contains("on"));
      resets.forEach((r) => press(r, !any && (r.dataset.reset === "all" ? n === list.length && n > 0 : n === 0)));
    }
    const ids = (b) => b.dataset.only.split(",").filter(Boolean);
    let applying = false;
    function apply() {
      const active = btns.filter((b) => b.getAttribute("aria-pressed") === "true");
      const m = new Set(active.flatMap(ids));
      applying = true;
      who().forEach((w) => { const v = m.has(w.dataset.pid); if (w.checked !== v) { w.checked = v; w.dispatchEvent(new Event("change", { bubbles: true })); } });
      applying = false;
      if (hint) hint.textContent = active.length
        ? "Seulement pour : " + active.map((b) => b.textContent.trim()).join(" + ") + " (" + m.size + " pers.). Clique encore un bouton pour l'ajouter ou le retirer."
        : "Clique un ou plusieurs boutons : seules leurs personnes sont cochées. Tu peux ensuite ajuster au cas par cas ci-dessous.";
    }
    btns.forEach((b) => b.addEventListener("click", () => {
      press(b, b.getAttribute("aria-pressed") !== "true");
      resets.forEach((r) => press(r, false));
      apply();
      syncResets();
    }));
    resets.forEach((r) => r.addEventListener("click", () => {
      btns.forEach((b) => press(b, false));
      applying = true;
      who().forEach((w) => { const v = r.dataset.reset === "all"; if (w.checked !== v) { w.checked = v; w.dispatchEvent(new Event("change", { bubbles: true })); } });
      applying = false;
      resets.forEach((x) => press(x, x === r));
      if (hint) hint.textContent = r.dataset.reset === "all" ? "Tout le monde est coché." : "Personne n'est coché.";
    }));
    function sameSet(a, b) { return a.size === b.size && [...a].every((x) => b.has(x)); }
    function syncFromChecks(manual) {   // les boutons allumés suivent les cases cochées
      const checked = new Set(who().filter((w) => w.checked).map((w) => w.dataset.pid));
      const active = btns.filter((b) => b.classList.contains("on"));
      if (active.length && sameSet(new Set(active.flatMap(ids)), checked)) return;   // toujours d'accord
      btns.forEach((b) => press(b, false));
      const match = checked.size > 1 && checked.size < who().length ? btns.slice().reverse().find((b) => sameSet(new Set(ids(b)), checked)) : null;
      if (match) press(match, true);
      if (!hint) return;
      if (match) hint.textContent = "Seulement pour : " + match.textContent.trim() + " (" + checked.size + " pers.). Clique encore un bouton pour l'ajouter ou le retirer.";
      else if (manual && active.length) hint.textContent = "Sélection ajustée à la main.";
    }
    form.addEventListener("change", (ev) => {     // ajustement à la main
      if (applying || !ev.target.matches("input[data-who]")) return;
      syncFromChecks(true);
      syncResets();
    });
    syncFromChecks(false);
    syncResets();
  });

  // Bulles d'explication : survol, clavier (focus) ou toucher
  let tipbox = null, tipFor = null, shownAt = 0;
  function showTip(el) {
    const src = el.querySelector(".tip-box");
    if (!src) return;
    if (!tipbox) { tipbox = document.createElement("div"); tipbox.id = "tipbox"; tipbox.setAttribute("role", "tooltip"); document.body.appendChild(tipbox); }
    if (tipFor !== el) shownAt = Date.now();
    tipbox.innerHTML = src.innerHTML; tipbox.hidden = false; tipFor = el;
    const r = el.getBoundingClientRect(), b = tipbox.getBoundingClientRect();
    let top = r.bottom + 6; if (top + b.height > innerHeight - 8) top = Math.max(8, r.top - b.height - 6);
    let left = r.right - b.width; if (left < 8) left = 8; if (left + b.width > innerWidth - 8) left = innerWidth - 8 - b.width;
    tipbox.style.top = top + "px"; tipbox.style.left = left + "px";
  }
  function hideTip() { if (tipbox) tipbox.hidden = true; tipFor = null; }
  document.addEventListener("mouseover", (ev) => { const t = ev.target.closest(".tip"); if (t && t !== tipFor) showTip(t); });
  document.addEventListener("mouseout", (ev) => { const t = ev.target.closest(".tip"); if (t && !t.contains(ev.relatedTarget) && document.activeElement !== t) hideTip(); });
  document.addEventListener("focusin", (ev) => { const t = ev.target.closest(".tip"); if (t) showTip(t); });
  document.addEventListener("focusout", (ev) => { if (ev.target.closest(".tip")) hideTip(); });
  document.addEventListener("click", (ev) => {
    const t = ev.target.closest(".tip");
    if (!t) return hideTip();
    if (t === tipFor && Date.now() - shownAt > 400) hideTip(); else showTip(t);   // un toucher = focus + clic
  });
  document.addEventListener("keydown", (ev) => { if (ev.key === "Escape") hideTip(); });
  addEventListener("scroll", hideTip, true);

  // Déplier / replier un groupe de lignes (compte d'une personne par catégorie)
  document.addEventListener("click", (ev) => {
    const b = ev.target.closest("[data-toggle-group]");
    const all = ev.target.closest("[data-toggle-all]");
    const set = (tb, open) => {
      tb.classList.toggle("collapsed", !open);
      const t = tb.querySelector("[data-toggle-group]");
      if (t) { t.setAttribute("aria-expanded", String(open)); t.querySelector(".chev").textContent = open ? "▾" : "▸"; }
    };
    if (b) { const tb = b.closest("tbody"); set(tb, tb.classList.contains("collapsed")); }
    if (all) (all.closest("[data-toggle-scope]") || document).querySelectorAll("tbody.acc-group").forEach((tb) => set(tb, all.dataset.toggleAll === "open"));
  });

  // Ouvrir / fermer une ligne de modification sous une ligne de tableau
  // (data-accordion : une seule ouverte à la fois dans la même carte)
  document.addEventListener("click", (ev) => {
    const b = ev.target.closest("[data-toggle-row]");
    if (!b) return;
    const row = document.querySelector(b.dataset.toggleRow);
    if (!row) return;
    if (b.hasAttribute("data-accordion") && row.hidden) {
      const card = b.closest("section, .card") || document;
      card.querySelectorAll("[data-accordion]").forEach((o) => {
        const r = document.querySelector(o.dataset.toggleRow);
        if (r && r !== row) { r.hidden = true; o.setAttribute("aria-expanded", "false"); }
      });
    }
    if (b.hasAttribute("data-reset") && row.reset) row.reset();      // « Annuler » : on oublie ce qui a été tapé
    row.hidden = !row.hidden;
    const opener = document.querySelector(`[data-toggle-row="${b.dataset.toggleRow}"][aria-expanded]`);
    document.querySelectorAll(`[data-toggle-row="${b.dataset.toggleRow}"][aria-expanded]`)
      .forEach((x) => x.setAttribute("aria-expanded", String(!row.hidden)));
    if (!row.hidden) { const f = row.querySelector("input[name=amount], [data-autofocus]"); if (f) f.focus(); }
    else if (opener && row.contains(document.activeElement || b)) opener.focus();   // fermé de l'intérieur : retour au bouton
  });

  // Enregistrement sans bouton (form[data-autosave]) : un champ s'enregistre quand on le quitte s'il a changé, ou avec
  // Entrée (envoi normal du formulaire) ; une case ou un choix dès qu'on le change. Après le rechargement, on revient
  // au même endroit de la page et au champ où l'on était allé (au clavier, Tab ne perd pas sa place).
  const AUTO_KEY = "autosave-retour";
  const FOCUSABLE = "a[href], button, input:not([type=hidden]), select, textarea";
  document.querySelectorAll("form[data-autosave]").forEach((f) => {
    const send = () => { if (f.requestSubmit) f.requestSubmit(); else f.submit(); };
    f.addEventListener("change", (ev) => { if (ev.target.matches("input[type=radio], input[type=checkbox], select")) send(); });
    f.addEventListener("focusout", (ev) => {
      const t = ev.target;
      if (t.matches("input[type=text], input[type=number]") && t.value !== t.defaultValue) send();
    });
    f.addEventListener("submit", (ev) => {
      if (f.dataset.sent) { ev.preventDefault(); return; }        // Entrée puis sortie du champ : un seul envoi
      f.dataset.sent = "1";
      addEventListener("pagehide", () => {
        const a = document.activeElement, all = [...document.querySelectorAll(FOCUSABLE)];
        try { sessionStorage.setItem(AUTO_KEY, JSON.stringify({path: location.pathname, y: scrollY,
          id: a && a.id ? a.id : "", n: a && a !== document.body ? all.indexOf(a) : -1})); } catch (e) {}
      }, {once: true});
    });
  });
  try {
    const back = JSON.parse(sessionStorage.getItem(AUTO_KEY) || "null");
    sessionStorage.removeItem(AUTO_KEY);
    if (back && back.path === location.pathname) {
      // après le chargement : le navigateur va d'abord à l'ancre de l'adresse (#resultat) et lâche le focus
      const put = () => {
        scrollTo(0, back.y);
        const el = (back.id && document.getElementById(back.id)) || (back.n >= 0 && document.querySelectorAll(FOCUSABLE)[back.n]);
        if (el) el.focus({preventScroll: true});
      };
      if (document.readyState === "complete") setTimeout(put, 0); else addEventListener("load", () => setTimeout(put, 0), {once: true});
    }
  } catch (e) {}

  // Bouton qui recharge la page sans la faire sauter (form[data-keep-place], « Retenir » une offre) : le bloc où l'on a
  // cliqué revient à la même hauteur dans la fenêtre, même si ce qui est au-dessus a grandi ou rétréci, et le focus
  // va au bouton qui a pris la place de celui qu'on a cliqué.
  const PLACE_KEY = "garder-place";
  document.querySelectorAll("form[data-keep-place]").forEach((f) => {
    f.addEventListener("submit", () => {
      const box = f.parentElement.closest("[id]");
      if (!box) return;
      try { sessionStorage.setItem(PLACE_KEY, JSON.stringify({path: location.pathname, id: box.id,
        top: box.getBoundingClientRect().top})); } catch (e) { return; }
      // retour sans ancre (sur_place=1) : sinon le navigateur affiche d'abord l'offre tout en haut, puis elle revient
      if (!/[?&]sur_place=/.test(f.action)) f.action += (f.action.includes("?") ? "&" : "?") + "sur_place=1";
    });
  });
  try {
    const was = JSON.parse(sessionStorage.getItem(PLACE_KEY) || "null");
    sessionStorage.removeItem(PLACE_KEY);
    const box = was && was.path === location.pathname && document.getElementById(was.id);
    if (!box) document.documentElement.classList.remove("placing");
    else {
      const put = () => {
        scrollBy(0, box.getBoundingClientRect().top - was.top);
        const b = box.querySelector("form[data-keep-place] button");
        if (b && document.activeElement !== b) b.focus({preventScroll: true});
      };
      put();                                               // tout de suite, avant le premier affichage
      document.documentElement.classList.remove("placing");
      // et encore après le chargement (images, polices : ce qui est au-dessus a pu changer de hauteur)
      if (document.readyState === "complete") setTimeout(put, 0); else addEventListener("load", () => setTimeout(put, 0), {once: true});
    }
  } catch (e) { document.documentElement.classList.remove("placing"); }   // jamais de page laissée cachée

  // Montants écrits sur les barres : seulement s'ils tiennent dans leur morceau de barre
  function fitSegNums() {
    document.querySelectorAll(".seg-num").forEach((n) => {
      n.classList.remove("hide");
      const r = document.createRange();
      r.selectNodeContents(n);
      if (r.getBoundingClientRect().width > n.parentElement.clientWidth - 8) n.classList.add("hide");
    });
  }
  if (document.querySelector(".seg-num")) {
    fitSegNums();
    let t = null;
    window.addEventListener("resize", () => { clearTimeout(t); t = setTimeout(fitSegNums, 100); });
  }

  // Lien direct vers un panneau repliable (…/asso#arrondi) : on l'ouvre
  if (location.hash) {
    const d = document.getElementById(location.hash.slice(1));
    if (d && d.tagName === "DETAILS") { d.open = true; d.scrollIntoView({block: "center"}); }
  }

  // Lien direct vers une ligne de modification (…#edit-m12) : on l'ouvre
  if (/^#edit-/.test(location.hash)) {
    const row = document.querySelector(location.hash);
    if (row && row.classList.contains("edit-row")) {
      row.hidden = false;
      document.querySelectorAll(`[data-toggle-row="${location.hash}"][aria-expanded]`).forEach((x) => x.setAttribute("aria-expanded", "true"));
      (row.previousElementSibling || row).scrollIntoView({block: "center"});
    }
  }

  // Afficher un bloc selon un choix radio : data-show-when="name=value"
  function syncShow() {
    document.querySelectorAll("[data-show-when]").forEach((el) => {
      const [name, values] = el.dataset.showWhen.split("=");
      const checked = document.querySelector(`input[name="${name}"]:checked`);
      const sel = document.querySelector(`select[name="${name}"]`);
      const v = checked ? checked.value : (sel ? sel.value : "");
      el.classList.toggle("hidden", !values.split("|").includes(v));
    });
  }
  document.addEventListener("change", syncShow);
  syncShow();

  // Envoyer son compte : télécharge le PDF puis ouvre WhatsApp (lien target=_blank) — <a data-send-pdf="/…pdf">
  document.addEventListener("click", (ev) => {
    const a = ev.target.closest("a[data-send-pdf]");
    if (!a) return;
    const d = document.createElement("a");
    d.href = a.dataset.sendPdf;
    d.download = "";
    document.body.appendChild(d);
    d.click();
    d.remove();
    const done = a.closest("[data-send-row]");
    if (done) done.classList.add("sent");
    if (window.pywebview) {   // dans sa propre fenêtre : dire où est le PDF (un navigateur l'affiche lui-même)
      let t = document.querySelector(".toast");
      if (!t) { t = document.createElement("div"); t.className = "toast"; t.setAttribute("role", "status"); document.body.appendChild(t); }
      t.textContent = "📄 PDF enregistré dans Téléchargements : joins-le au message.";
      t.hidden = false;
      clearTimeout(t._h); t._h = setTimeout(() => { t.hidden = true; }, 7000);
    }
  });

  // Relance : pas de PDF à joindre, la ligne est simplement notée « préparé » — <a data-sent>, <button data-sent>
  document.addEventListener("click", (ev) => {
    const a = ev.target.closest("[data-sent]");
    const row = a && a.closest("[data-send-row]");
    if (row) row.classList.add("sent");
  });

  // Aperçu des fichiers choisis (photos et PDF) avant l'envoi, avec un × pour en retirer un
  document.querySelectorAll("input[type=file][data-file-preview]").forEach((inp) => {
    // Chaque nouveau choix S'AJOUTE aux fichiers déjà choisis (le navigateur, lui, remplacerait la sélection)
    let chosen = [];
    inp.required = false;
    inp.classList.add("sr-only");
    const pick = document.createElement("button");
    pick.type = "button"; pick.className = "btn small ghost file-pick";
    pick.addEventListener("click", () => inp.click());
    inp.insertAdjacentElement("afterend", pick);
    const box = document.createElement("div");
    box.className = "file-preview";
    box.setAttribute("aria-live", "polite");
    pick.insertAdjacentElement("afterend", box);
    let urls = [];
    function sync() {
      const dt = new DataTransfer();
      chosen.forEach((f) => dt.items.add(f));
      inp.files = dt.files;
      pick.textContent = chosen.length ? "+ Ajouter un autre fichier" : "📎 Choisir une photo ou un PDF";
      draw();
    }
    function draw() {
      urls.forEach((u) => URL.revokeObjectURL(u)); urls = [];
      box.innerHTML = "";
      Array.from(inp.files).forEach((f, i) => {
        const u = URL.createObjectURL(f); urls.push(u);
        const card = document.createElement("div"); card.className = "fp";
        const b = document.createElement("div"); b.className = "fp-box";
        if (f.type.startsWith("image/")) { const im = document.createElement("img"); im.src = u; im.alt = f.name; b.appendChild(im); }
        else if (f.type === "application/pdf" || /\.pdf$/i.test(f.name)) {
          b.textContent = "PDF…"; b.classList.add("is-pdf");
          const fd = new FormData(); fd.append("file", f);
          fetch("/apercu-fichier", { method: "POST", body: fd }).then((r) => r.ok ? r.blob() : null).then((bl) => {
            if (!bl || bl.type !== "image/jpeg") { b.textContent = "PDF"; return; }
            const im = document.createElement("img"); im.alt = f.name; im.src = URL.createObjectURL(bl);
            b.textContent = ""; b.appendChild(im);
          }).catch(() => { b.textContent = "PDF"; });
        }
        else { b.textContent = f.name.split(".").pop().toUpperCase(); }
        const n = document.createElement("div"); n.className = "fp-name"; n.textContent = f.name; n.title = f.name;
        const x = document.createElement("button"); x.type = "button"; x.className = "fp-x"; x.textContent = "×";
        x.setAttribute("aria-label", "Retirer " + f.name);
        x.addEventListener("click", () => { chosen.splice(i, 1); sync(); pick.focus(); });
        card.append(b, n, x); box.appendChild(card);
      });
    }
    inp.addEventListener("change", () => {
      const key = (f) => f.name + "|" + f.size + "|" + f.lastModified;
      const have = new Set(chosen.map(key));
      Array.from(inp.files).forEach((f) => { if (!have.has(key(f))) { chosen.push(f); have.add(key(f)); } });
      sync();
    });
    sync();
  });

  // Zone de texte qui s'agrandit avec son contenu (au moins sa hauteur de départ)
  function grow(t) { t.style.height = "auto"; t.style.height = Math.max(t.scrollHeight + 2, t.dataset.minH || 0) + "px"; }
  document.querySelectorAll("textarea[data-autogrow]").forEach((t) => { t.dataset.minH = t.offsetHeight; grow(t); });
  document.addEventListener("input", (ev) => { if (ev.target.matches("textarea[data-autogrow]")) grow(ev.target); });

  // Un formulaire en cours de saisie n'est jamais rechargé automatiquement
  document.addEventListener("input", (ev) => { const f = ev.target.closest("form"); if (f) f.dataset.dirty = "1"; });
  document.addEventListener("submit", (ev) => { if (!ev.defaultPrevented) delete ev.target.dataset.dirty; });

  // Quitter la page alors qu'un formulaire est commencé : on prévient (lien du menu, bouton « Arrêter »…).
  // Seuls comptent les formulaires qui enregistrent (method=post) et qui sont visibles.
  const LEAVE = "Tu as commencé à remplir un formulaire sur cette page.\n\nQuitter sans l'enregistrer ?";
  const unsaved = (except) => [...document.querySelectorAll("form[data-dirty]")].some((f) =>
    f !== except && (f.getAttribute("method") || "").toLowerCase() === "post" && !f.hasAttribute("data-no-guard") &&
    f.checkVisibility());
  document.addEventListener("click", (ev) => {
    const a = ev.target.closest("a[href]");
    if (!a || ev.defaultPrevented || a.target === "_blank" || a.hasAttribute("download") || a.dataset.sendPdf ||
        a.hasAttribute("data-check") || a.hasAttribute("data-focus")) return;
    const href = a.getAttribute("href");
    if (href.startsWith("#") || /^(mailto|tel|javascript):/i.test(href) || /\.(pdf|zip|csv|txt|md)(\?|$)/i.test(href)) return;
    // un lien du formulaire lui-même (« Annuler », « Pas maintenant ») : c'est un abandon voulu, pas de question
    if (unsaved(a.closest("form")) && !confirm(LEAVE)) ev.preventDefault();
  });
  document.addEventListener("submit", (ev) => {            // « Arrêter », « Verrouiller » : quitter aussi
    const f = ev.target;
    if (!ev.defaultPrevented && (f.matches(".quit") || /\/verrouiller$/.test(f.getAttribute("action") || "")) &&
        unsaved(f) && !confirm(LEAVE)) ev.preventDefault();
  }, true);

  // Bouton « Aujourd'hui » à côté de chaque date (sauf les dates du voyage : data-no-today). Un champ date vide
  // n'est jamais rempli tout seul : sur Mac, la date du jour qu'on y voit en gris n'est qu'un décor. Au clavier,
  // Tab accepte cette proposition (comme on accepte une suggestion grisée), Échap la refuse (« sans date ») ; à la
  // souris, le bouton. Cliquer ailleurs, revenir en arrière (Maj+Tab) ou taper une date à la main ne remplit rien.
  document.querySelectorAll('input[type="date"]:not([data-no-today])').forEach((d) => {
    const b = document.createElement("button");
    let typing = false;       // des chiffres ont été tapés : on laisse la personne finir sa date
    let refused = false;      // Échap : « sans date », Tab ne propose plus rien
    let accepted = "";        // la date que Tab vient de mettre (Échap la retire)
    const iso = () => {
      const n = new Date(), p = (x) => String(x).padStart(2, "0");
      return n.getFullYear() + "-" + p(n.getMonth() + 1) + "-" + p(n.getDate());
    };
    const set = (v) => {
      d.value = v;
      d.dispatchEvent(new Event("input", { bubbles: true }));
      d.dispatchEvent(new Event("change", { bubbles: true }));
    };
    const offered = () => !d.value && !typing && !refused && !(d.validity && d.validity.badInput);
    const hint = () => {
      const text = offered() ? "Tab : aujourd'hui · Échap : sans date"
        : (accepted && d.value === accepted) ? "Échap : retirer la date" : "";
      if (text) b.dataset.hint = text; else delete b.dataset.hint;
    };
    b.type = "button"; b.className = "today-btn"; b.textContent = "Aujourd'hui";
    b.setAttribute("aria-label", "Mettre la date d'aujourd'hui");
    b.title = "Au clavier : Tab, dans le champ date vide";
    b.addEventListener("click", () => { accepted = ""; set(iso()); hint(); });
    d.addEventListener("focus", () => { if (!d.value && !(d.validity && d.validity.badInput)) typing = false; hint(); });
    d.addEventListener("input", () => { if (d.value !== accepted) accepted = ""; hint(); });
    d.addEventListener("keydown", (ev) => {
      if (ev.metaKey || ev.ctrlKey || ev.altKey) return;
      if (ev.key === "Tab" && !ev.shiftKey && offered()) {
        accepted = iso();
        set(accepted);                       // pas de preventDefault : Tab continue son chemin
      } else if (ev.key === "Escape" && (offered() || (accepted && d.value === accepted))) {
        ev.preventDefault();
        refused = true; accepted = "";
        if (d.value) set("");
      } else if (/^[0-9]$/.test(ev.key) || ev.key === "ArrowUp" || ev.key === "ArrowDown") {
        typing = true;
      }
      hint();
    });
    d.insertAdjacentElement("afterend", b);
  });

  // Lien qui amène à un champ : <a href="#id" data-focus="#id">
  document.addEventListener("click", (ev) => {
    const a = ev.target.closest("[data-focus]");
    const el = a && document.querySelector(a.dataset.focus);
    if (!el) return;
    ev.preventDefault();
    el.scrollIntoView({ block: "center" });
    el.focus();
  });

  // Formulaire refusé : il revient avec ce qui avait été saisi, et le message à côté du champ en cause.
  // À l'envoi, la saisie est gardée (le temps de l'aller-retour) ; si la page revient avec un refus (?e=1), elle est
  // remise dans le même formulaire. Les fichiers choisis ne peuvent pas être remis : il faut les rechoisir.
  const RETRY = "formulaire-refuse";
  const pathOf = (f) => { try { return new URL(f.getAttribute("action") || "", location.href).pathname; } catch (e) { return ""; } };
  const formsAt = (path) => [...document.querySelectorAll("form")].filter((f) => pathOf(f) === path);
  const checkable = (el) => el.type === "checkbox" || el.type === "radio";
  document.addEventListener("submit", (ev) => {
    const f = ev.target;
    if (ev.defaultPrevented || (f.getAttribute("method") || "").toLowerCase() !== "post" || f.hasAttribute("data-no-retry")) return;
    const fields = [...f.elements].filter((el) => el.name && !["file", "hidden", "password", "submit", "button"].includes(el.type))
      .map((el) => (checkable(el) ? [el.name, el.value, el.checked, 1] : [el.name, el.value, false, 0]));
    try {
      sessionStorage.setItem(RETRY, JSON.stringify({ action: pathOf(f), idx: formsAt(pathOf(f)).indexOf(f), fields, t: Date.now() }));
    } catch (e) {}
  });
  (function refusedForm() {
    let saved = null;
    try { saved = JSON.parse(sessionStorage.getItem(RETRY) || "null"); sessionStorage.removeItem(RETRY); } catch (e) {}
    const q = new URLSearchParams(location.search);
    if (q.get("e") !== "1") return;
    let form = null;
    if (saved && Date.now() - saved.t < 600000) {
      const same = formsAt(saved.action);
      form = same[saved.idx] || (same.length === 1 ? same[0] : null);
    }
    if (form) {
      const fire = (el) => { el.dispatchEvent(new Event("input", { bubbles: true })); el.dispatchEvent(new Event("change", { bubbles: true })); };
      const els = [...form.elements].filter((el) => el.name && !["file", "hidden", "password"].includes(el.type));
      saved.fields.filter((x) => x[3]).forEach(([name, value, checked]) => {          // d'abord les cases et les choix
        const el = els.find((x) => checkable(x) && x.name === name && x.value === value);
        if (el && el.checked !== checked) { el.checked = checked; fire(el); }
      });
      const nth = {};
      saved.fields.filter((x) => !x[3]).forEach(([name, value]) => {                  // puis les textes, dans l'ordre
        nth[name] = nth[name] || 0;
        const el = els.filter((x) => !checkable(x) && x.name === name)[nth[name]++];
        if (el && el.value !== value) { el.value = value; fire(el); }
      });
      form.dataset.dirty = "1";
      for (let n = form; n; n = n.parentElement) {                                   // formulaire replié : on l'ouvre
        if (n.hidden) n.hidden = false;
        if (n.tagName === "DETAILS") n.open = true;
      }
    }
    const msg = q.get("msg") || "", name = q.get("f");
    const scope = form || document;
    const target = name && [...scope.querySelectorAll('[name="' + name.replace(/["\\]/g, "") + '"]')]
      .find((x) => x.type !== "hidden" && x.checkVisibility());
    if (!msg || (!target && !form)) return;
    const note = document.createElement("p");
    note.className = "field-err"; note.id = "champ-refuse"; note.setAttribute("role", "alert");
    note.textContent = "⚠️ " + msg;
    if (target) {
      target.setAttribute("aria-invalid", "true");
      target.setAttribute("aria-describedby", note.id);
      (target.closest(".field") || target.parentElement).appendChild(note);
      target.focus({ preventScroll: true });
      target.scrollIntoView({ block: "center" });
      target.addEventListener("input", () => { target.removeAttribute("aria-invalid"); note.remove(); }, { once: true });
    } else {
      form.prepend(note);
      form.scrollIntoView({ block: "nearest" });
    }
  })();

  // Après un redémarrage de l'application (mise à jour), l'onglet se recharge tout seul
  (function watchRestart() {
    if (document.querySelector(".update-steps")) return;   // la page d'attente de la mise à jour s'en charge
    let boot = null, down = false;
    async function tick() {
      try {
        const r = await fetch("/ping", { cache: "no-store" });
        const j = await r.json();
        if (boot && j.boot !== boot) {
          const dirty = document.querySelector("form[data-dirty]");
          if (!dirty) { location.reload(); return; }
        }
        boot = boot || j.boot; down = false;
      } catch (e) { down = true; }
      setTimeout(tick, down ? 1500 : 4000);
    }
    setTimeout(tick, 1500);
  })();

  // « Aucun » : exclusif avec les autres cases du même ensemble ; coché d'office si rien d'autre ne l'est
  document.addEventListener("change", (ev) => {
    const box = ev.target.closest("[data-none-group]");
    if (!box || ev.target.type !== "checkbox") return;
    const none = box.querySelector("input[data-none]");
    const others = Array.from(box.querySelectorAll("input[type=checkbox]:not([data-none])"));
    if (ev.target === none) { if (none.checked) others.forEach((c) => { c.checked = false; }); else if (!others.some((c) => c.checked)) none.checked = true; }
    else none.checked = !others.some((c) => c.checked);
  });

  // Message retouché avant l'envoi : <textarea data-msg-edit> — WhatsApp, e-mail, « Copier » et « Autre… » suivent
  const applyMsg = (ta) => {
    const row = ta.closest("[data-send-row]");
    if (!row) return;
    const text = ta.value.trim(), enc = encodeURIComponent(text);
    const wa = row.querySelector('.share-menu a[href^="https://wa.me/"]');
    if (wa) wa.href = "https://wa.me/" + (ta.dataset.num || "") + "?text=" + enc;
    const mail = row.querySelector('.share-menu a[href^="mailto:"]');
    if (mail) mail.href = "mailto:" + encodeURIComponent(ta.dataset.mail || "").replace(/%40/g, "@") +
      "?subject=" + encodeURIComponent(ta.dataset.subject || "") + "&body=" + enc;
    const tpl = row.querySelector("[data-share] template");
    if (tpl) tpl.content.textContent = text;
    const reset = row.querySelector("[data-msg-reset]");
    if (reset) reset.hidden = ta.value === ta.defaultValue;
  };
  document.addEventListener("input", (ev) => { if (ev.target.matches("[data-msg-edit]")) applyMsg(ev.target); });
  document.addEventListener("click", (ev) => {
    const b = ev.target.closest("[data-msg-reset]");
    if (!b) return;
    const ta = b.closest("[data-send-row]").querySelector("[data-msg-edit]");
    ta.value = ta.defaultValue;
    applyMsg(ta);
  });

  // Copier un texte : <button data-copy="#id"> (élément ou <template>)
  const textOf = (el) => (el.content ? el.content.textContent : el.textContent).trim();
  document.addEventListener("click", async (ev) => {
    const b = ev.target.closest("[data-copy]");
    if (!b) return;
    const el = document.querySelector(b.dataset.copy);
    if (!el) return;
    const label = b.querySelector("b") || b;
    try { await navigator.clipboard.writeText(textOf(el)); label.textContent = "✓ Copié"; }
    catch (e) { label.textContent = "Copie impossible"; }
  });

  // Menu « Partager » : <div data-share> bouton + menu (WhatsApp, e-mail, partage du système, PDF, copier)
  const closeShares = (except) => document.querySelectorAll("[data-share] .share-menu:not([hidden])").forEach((m) => {
    if (m === except) return;
    m.hidden = true; m.previousElementSibling.setAttribute("aria-expanded", "false");
  });
  document.addEventListener("click", (ev) => {
    const btn = ev.target.closest(".share-btn");
    if (btn) {
      const menu = btn.nextElementSibling;
      closeShares(menu);
      menu.hidden = !menu.hidden;
      btn.setAttribute("aria-expanded", String(!menu.hidden));
      return;
    }
    const item = ev.target.closest(".share-menu [role=menuitem]");
    if (item && !item.matches("[data-copy]")) { closeShares(); return; }
    if (!ev.target.closest("[data-share]")) closeShares();
  });
  document.addEventListener("keydown", (ev) => { if (ev.key === "Escape") closeShares(); });
  // « Autre… » : partage du système avec le PDF joint, seulement si le navigateur sait partager un fichier
  const canShareFiles = (() => { try { return !!(navigator.canShare && navigator.canShare({ files: [new File(["x"], "x.pdf", { type: "application/pdf" })] })); } catch (e) { return false; } })();
  document.querySelectorAll("[data-share-native]").forEach((b) => { b.hidden = !canShareFiles; });
  document.addEventListener("click", async (ev) => {
    const b = ev.target.closest("[data-share-native]");
    if (!b) return;
    try {
      const r = await fetch(b.dataset.pdf);
      const name = decodeURIComponent(((r.headers.get("content-disposition") || "").match(/filename\*=UTF-8''([^;]+)/) || [])[1] || "compte.pdf");
      const file = new File([await r.blob()], name, { type: "application/pdf" });
      const el = document.querySelector(b.dataset.msg);
      await navigator.share({ files: [file], title: b.dataset.title, text: el ? textOf(el) : "" });
      const row = b.closest("[data-send-row]"); if (row) row.classList.add("sent");
    } catch (e) { /* partage annulé */ }
  });

  // Sélecteur qui recharge la page : <select data-nav="/url?p=">
  document.querySelectorAll("select[data-nav]").forEach((s) => {
    s.addEventListener("change", () => { location.href = s.dataset.nav + encodeURIComponent(s.value); });
  });

  // Participants déjà venus sur d'autres voyages : recherche, « tout le groupe », compteur ; remplissage du formulaire
  const personKey = (n) => (n || "").normalize("NFKD").replace(/[^\x00-\x7f]/g, "").toLowerCase().split(/\s+/).filter(Boolean).join(" ");
  document.querySelectorAll("[data-known]").forEach((form) => {
    const items = [...form.querySelectorAll(".known-list label")];
    const boxes = items.map((l) => l.querySelector("input"));
    const submit = form.querySelector("[data-known-submit]");
    const count = () => {
      const n = boxes.filter((b) => b.checked).length;
      submit.disabled = !n;
      submit.textContent = n ? `Ajouter ${n === 1 ? "la personne cochée" : `les ${n} personnes cochées`}` : "Ajouter les personnes cochées";
    };
    form.addEventListener("change", count);
    const filter = form.querySelector("[data-known-filter]");
    filter.addEventListener("input", () => {
      const q = personKey(filter.value);
      let shown = 0;
      items.forEach((l) => { const ok = !q || l.dataset.name.includes(q); l.hidden = !ok; shown += ok; });
      form.querySelector("[data-known-none]").hidden = shown > 0;
    });
    filter.addEventListener("keydown", (ev) => { if (ev.key === "Enter") ev.preventDefault(); });
    form.querySelectorAll("[data-known-trip]").forEach((b) => b.addEventListener("click", () => {
      const mine = items.filter((l) => l.dataset.trips.includes(` ${b.dataset.knownTrip} `)).map((l) => l.querySelector("input"));
      const all = mine.every((x) => x.checked);                  // un 2e clic décoche le groupe
      mine.forEach((x) => { x.checked = !all; });
      count();
    }));
  });
  document.querySelectorAll("[data-known-fill]").forEach((form) => {
    let fill = {};
    try { fill = JSON.parse(form.dataset.knownFill); } catch (e) { return; }
    const name = form.querySelector("input[name=name]");
    name.addEventListener("change", () => {
      const k = fill[personKey(name.value)];
      if (!k) return;
      for (const f of ["phone", "email"]) {
        const inp = form.querySelector(`input[name=${f}]`);
        if (inp && !inp.value && k[f]) inp.value = k[f];
      }
      const role = form.querySelector(`input[name=r_${k.companion ? "accompagnant" : "plongeur"}]`);
      if (role && !role.checked) { role.checked = true; role.dispatchEvent(new Event("change", { bubbles: true })); }
    });
  });
})();

// Trip Compta dans sa propre fenêtre (pas un navigateur) : Cmd/Ctrl+F cherche dans la page, Cmd/Ctrl+N ouvre une
// nouvelle fenêtre. Dans un navigateur, rien ne change (ses propres raccourcis restent).
(function () {
  function inWindow() { return !!window.pywebview; }
  let bar = null, input = null, last = "";
  function search(back) {
    const q = input.value;
    if (!q) return;
    if (q !== last) { last = q; window.getSelection().removeAllRanges(); }
    // le texte de la page (sans les champs de saisie : sinon on « trouverait » ce qu'on vient de taper)
    const inPage = document.body.innerText.toLowerCase().includes(q.toLowerCase());
    const found = inPage && window.find(q, false, back, true, false, false, false);
    bar.classList.toggle("none", !found);
    input.focus();
  }
  window.tcFind = function () {
    if (!bar) {
      bar = document.createElement("div");
      bar.className = "find-bar";
      bar.setAttribute("role", "search");
      bar.innerHTML = '<label class="sr-only" for="tc-find">Rechercher dans la page</label>' +
        '<input id="tc-find" type="search" placeholder="Rechercher dans la page" autocomplete="off">' +
        '<button type="button" class="iconbtn" data-dir="back" title="Précédent (Maj+Entrée)">↑</button>' +
        '<button type="button" class="iconbtn" data-dir="next" title="Suivant (Entrée)">↓</button>' +
        '<button type="button" class="iconbtn" data-close title="Fermer (Échap)">✕</button>' +
        '<span class="find-none small">Introuvable</span>';
      document.body.appendChild(bar);
      input = bar.querySelector("input");
      input.addEventListener("keydown", (e) => {
        if (e.key === "Enter") { e.preventDefault(); search(e.shiftKey); }
        if (e.key === "Escape") { e.preventDefault(); bar.hidden = true; }
      });
      bar.querySelector("[data-dir=back]").addEventListener("click", () => search(true));
      bar.querySelector("[data-dir=next]").addEventListener("click", () => search(false));
      bar.querySelector("[data-close]").addEventListener("click", () => { bar.hidden = true; });
    }
    bar.hidden = false;
    const sel = String(window.getSelection() || "").trim();
    if (sel && sel.length < 80) input.value = sel;
    input.focus();
    input.select();
  };
  // éléments réservés à la fenêtre de l'app (data-window-only) ; pywebview arrive après
  function showWindowOnly() { document.querySelectorAll("[data-window-only]").forEach((n) => { n.hidden = !inWindow(); }); }
  showWindowOnly();
  window.addEventListener("pywebviewready", showWindowOnly);
  // Voyage clôturé : les formulaires qui modifient sont éteints (le serveur les refuse de toute façon)
  const closed = document.querySelector("main[data-closed]");
  if (closed) {
    closed.querySelectorAll('form[method="post" i]:not([data-closed-ok])').forEach((f) => {
      if (/apercu$/.test(f.getAttribute("action") || "")) return;
      f.querySelectorAll("button, input, select, textarea").forEach((el) => {
        el.disabled = true;
        if (el.tagName === "BUTTON") el.title = "Voyage clôturé : rouvre-le pour le modifier";
      });
    });
    closed.querySelectorAll("input[form]").forEach((el) => { el.disabled = true; });   // cases liées à un formulaire
    // … et les boutons qui ne font qu'ouvrir un de ces formulaires (« ✓ Reçu », « Modifier »…)
    const off = 'form[method="post" i]:not([data-closed-ok])';
    closed.querySelectorAll("[data-toggle-row]").forEach((b) => {
      let t = null;
      try { t = document.querySelector(b.dataset.toggleRow); } catch (e) { /* sélecteur inattendu : on laisse */ }
      if (t && (t.matches(off) || t.querySelector(off) || t.closest(off))) {
        b.disabled = true;
        b.title = "Voyage clôturé : rouvre-le pour le modifier";
      }
    });
  }
  // Ctrl/Cmd + Z, hors d'un champ de saisie (où il annule la frappe) : les dernières modifications, pour défaire
  document.addEventListener("keydown", (e) => {
    if (!(e.metaKey || e.ctrlKey) || e.altKey || e.shiftKey || e.key.toLowerCase() !== "z") return;
    const el = document.activeElement;
    if (el && (el.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName))) return;
    if (!document.querySelector(".undo-btn") || location.pathname === "/historique") return;
    e.preventDefault();
    location.href = "/historique";
  });
  document.addEventListener("keydown", (e) => {
    if (!inWindow() || !(e.metaKey || e.ctrlKey) || e.altKey) return;
    const k = e.key.toLowerCase();
    if (k === "f") { e.preventDefault(); window.tcFind(); }
    if (k === "n") { e.preventDefault(); fetch("/fenetre/nouvelle", { method: "POST" }); }
  });
})();

// Hauteur réelle de l'en-tête (il peut passer sur deux lignes quand la fenêtre est étroite) : le menu du voyage,
// fixe quand on fait défiler, se cale juste dessous, sans bande où l'on verrait le contenu passer.
(function () {
  const top = document.querySelector(".topbar");
  if (!top) return;
  const set = () => document.documentElement.style.setProperty("--topbar-h", top.getBoundingClientRect().height + "px");
  set();
  if (window.ResizeObserver) new ResizeObserver(set).observe(top);
  else window.addEventListener("resize", set);
})();

// « Reste à régler » : cocher plusieurs lignes, puis tout noter d'un coup (chaque ligne pour son montant exact).
(function () {
  const euro = (c) => (c / 100).toLocaleString("fr-FR", { minimumFractionDigits: 2, maximumFractionDigits: 2 }) + "\u00a0€";
  document.querySelectorAll("form[data-batch]").forEach((form) => {
    const boxes = [...document.querySelectorAll('input[type=checkbox][form="' + form.id + '"]')];
    const all = form.querySelector("[data-batch-all]"), go = form.querySelector("[data-batch-go]");
    const none = form.querySelector("[data-batch-none]"), some = form.querySelector("[data-batch-some]");
    function sync() {
      const on = boxes.filter((b) => b.checked);
      const total = on.reduce((a, b) => a + (+b.dataset.amount || 0), 0);
      none.hidden = on.length > 0;
      some.hidden = on.length === 0;
      go.textContent = go.dataset.label + " : " + on.length + (on.length > 1 ? " lignes, " : " ligne, ") + euro(total);
      all.checked = on.length === boxes.length;
      all.indeterminate = on.length > 0 && on.length < boxes.length;
      boxes.forEach((b) => { const tr = b.closest("tr"); if (tr) tr.classList.toggle("ticked", b.checked); });
    }
    boxes.forEach((b) => b.addEventListener("change", sync));
    all.addEventListener("change", () => { boxes.forEach((b) => { if (!b.disabled) b.checked = all.checked; }); sync(); });
    window.addEventListener("pageshow", sync);
    sync();
  });
})();

// Dépenses : chercher, filtrer (payé par, état) sans recharger la page. Les totaux affichés suivent ce qui est visible.
(function () {
  const box = document.querySelector("[data-exp-filter]");
  if (!box) return;
  const q = box.querySelector("#exp-q"), payer = box.querySelector("#exp-payer"), state = box.querySelector("#exp-state");
  const count = box.querySelector("#exp-count");
  const rows = [...document.querySelectorAll("tr[data-exp]")];
  const norm = (s) => (s || "").toLowerCase().normalize("NFD").replace(/[\u0300-\u036f]/g, "");
  const euro = (c) => (c / 100).toLocaleString("fr-FR", { minimumFractionDigits: 2, maximumFractionDigits: 2 }) + "\u00a0€";
  const sum = (list) => list.reduce((a, tr) => a + (+tr.dataset.amount || 0), 0);
  function total(el, list, active) {
    if (!el) return;
    if (el.dataset.full === undefined) el.dataset.full = el.textContent;
    el.textContent = active ? euro(sum(list)) : el.dataset.full;
  }
  function apply() {
    const words = norm(q.value).split(/\s+/).filter(Boolean), p = payer.value, st = state.value;
    const active = !!(words.length || p || st);
    rows.forEach((tr) => {
      const text = norm(tr.dataset.q);
      tr.hidden = !(words.every((w) => text.includes(w))
        && (!p || tr.dataset.payers.split(" ").includes(p)) && (!st || tr.dataset.state.split(" ").includes(st)));
    });
    document.querySelectorAll("tr.subcat-row").forEach((h) => {       // sous-catégorie : ses lignes encore visibles
      const mine = [];
      for (let n = h.nextElementSibling; n && !n.classList.contains("subcat-row"); n = n.nextElementSibling) {
        if (n.hasAttribute("data-exp") && !n.hidden) mine.push(n);
      }
      h.hidden = !mine.length;
      total(h.querySelector("[data-exp-total]"), mine, active);
    });
    document.querySelectorAll("details[data-exp-group]").forEach((d) => {
      const mine = [...d.querySelectorAll("tr[data-exp]")].filter((tr) => !tr.hidden);
      d.hidden = !mine.length;
      total(d.querySelector("summary [data-exp-total]"), mine, active);
    });
    const shown = rows.filter((tr) => !tr.hidden);
    count.hidden = !active;
    count.querySelector("span").textContent = !shown.length ? "Aucune dépense ne correspond."
      : shown.length + (shown.length > 1 ? " dépenses affichées" : " dépense affichée") + " sur " + rows.length
        + " · " + euro(sum(shown));
    box.classList.toggle("filtering", active);
  }
  [q, payer, state].forEach((el) => { el.addEventListener("input", apply); el.addEventListener("change", apply); });
  box.querySelector("[data-exp-reset]").addEventListener("click", () => { q.value = ""; payer.value = ""; state.value = ""; apply(); q.focus(); });
  window.addEventListener("pageshow", apply);          // retour arrière : le navigateur a pu remettre les champs
  apply();
})();
