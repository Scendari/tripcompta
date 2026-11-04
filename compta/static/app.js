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
    row.hidden = !row.hidden;
    document.querySelectorAll(`[data-toggle-row="${b.dataset.toggleRow}"][aria-expanded]`)
      .forEach((x) => x.setAttribute("aria-expanded", String(!row.hidden)));
    if (!row.hidden) { const f = row.querySelector("input[name=amount]"); if (f) f.focus(); }
  });

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
  document.addEventListener("submit", (ev) => { delete ev.target.dataset.dirty; });

  // Après un redémarrage de l'application (mise à jour), l'onglet se recharge tout seul
  (function watchRestart() {
    if (document.querySelector(".update-steps")) return;   // la page d'attente de la mise à jour s'en charge
    let boot = null, down = false;
    async function tick() {
      try {
        const r = await fetch("/ping?vu=1", { cache: "no-store" });   // « vu » : un onglet est ouvert (cloud.py)
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
    if (ev.target.dataset.pair && ev.target.checked) {          // Plongeur / Accompagnant : l'un ou l'autre
      box.querySelectorAll(`input[data-pair="${ev.target.dataset.pair}"]`).forEach((c) => { if (c !== ev.target) c.checked = false; });
    }
    if (ev.target === none) { if (none.checked) others.forEach((c) => { c.checked = false; }); else if (!others.some((c) => c.checked)) none.checked = true; }
    else none.checked = !others.some((c) => c.checked);
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
  document.addEventListener("keydown", (e) => {
    if (!inWindow() || !(e.metaKey || e.ctrlKey) || e.altKey) return;
    const k = e.key.toLowerCase();
    if (k === "f") { e.preventDefault(); window.tcFind(); }
    if (k === "n") { e.preventDefault(); fetch("/fenetre/nouvelle", { method: "POST" }); }
  });
})();
