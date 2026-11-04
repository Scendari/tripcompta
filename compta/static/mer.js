// Trip Compta — « Mes voyages » : surprises de la mer (baleine, dauphins, mouette…).
// Rythme : à l'arrivée, une chance sur deux qu'une surprise passe 4 à 8 s après ; puis une toutes les 40 à 70 s
// tant qu'on reste. Jamais deux fois la même d'affilée (mémoire des dernières vues), coucher de soleil le soir,
// phare en thème sombre, « tout est réglé » seulement quand tous les voyages le sont (une fois).
// Rien si « réduire les animations », si la fenêtre est cachée, ou si un formulaire de l'accueil est ouvert.
(function () {
  const LIFE = document.getElementById("sea-life"), under = document.getElementById("sea-under");
  const HARBOUR = document.querySelector(".harbour");
  if (!LIFE || !under || !HARBOUR) return;
  let life = LIFE, harbour = HARBOUR;          // pendant une surprise « dans l'autre sens » : calques retournés
  let busy = false;
  function spawn(id, style, layer) {
    const n = document.getElementById(id).content.firstElementChild.cloneNode(true);
    Object.entries(style || {}).forEach(([k, v]) => n.style.setProperty(k, v)); (layer || life).appendChild(n);
    requestAnimationFrame(() => n.classList.add("go"));
    return n;
  }
  function done(nodes, ms) { setTimeout(() => { nodes.forEach((n) => n.remove()); busy = false; }, ms); }
  const rnd = (a, b) => a + Math.random() * (b - a);
  const EGGS = {
    whale() {   // jamais sur un bateau : entre le € et le =, ou entre le = et le +
      const x = Math.random() < .5 ? rnd(20, 32) : rnd(57, 70);
      const w = spawn("t-whale", { left: x + "vw" }, under); done([w], 9300); },
    dolphins() {
      const x = rnd(12, 55), jx = rnd(60, 80) + "px", jy = rnd(42, 56) + "px", nodes = [];
      [0, .4].forEach((dl) => {
        nodes.push(spawn("t-dolphin", { left: x + "vw", "--dl": dl + "s", "--jx": jx, "--jy": jy }));
        nodes.push(spawn("t-splash", { left: `calc(${x}vw + 14px)`, "--dl": dl + "s" }));
        nodes.push(spawn("t-splash", { left: `calc(${x}vw + 14px + 2 * ${jx})`, "--dl": (dl + 1.9) + "s" }));
      });
      done(nodes, 3400);
    },
    gull() { const g = spawn("t-gull", { bottom: rnd(100, 118) + "px" }); done([g], 14300); },
    bottle() { const b = spawn("t-bottle"); done([b], 28300); },

    // 1. phare au loin : son faisceau balaie la mer (surtout joli en thème sombre)
    lighthouse() {
      const l = spawn("t-lighthouse");
      l.animate([{ opacity: 0 }, { opacity: 1, offset: .1 }, { opacity: 1, offset: .88 }, { opacity: 0 }], { duration: 16000, fill: "forwards" });
      done([l], 16200);
    },
    // 2. poisson volant : trois petits bonds au ras de l'eau
    flyingfish() {
      const f = spawn("t-ffish", { left: rnd(15, 45) + "vw" }), k = [], hop = 78, nodes = [f];
      for (let i = 0; i < 3; i++) {
        k.push({ transform: `translate(${i * hop}px, 0) rotate(-24deg)`, opacity: i ? 1 : 0, offset: i / 3 });
        k.push({ transform: `translate(${i * hop + hop / 2}px, -34px) rotate(0deg)`, opacity: 1, offset: (i + .5) / 3 });
      }
      k.push({ transform: `translate(${3 * hop}px, 0) rotate(26deg)`, opacity: 0, offset: 1 });
      f.animate(k, { duration: 2700, easing: "linear", fill: "forwards" });
      done(nodes, 2900);
    },
    // 3. tortue : sort la tête, regarde autour d'elle, replonge
    turtle() {
      const t = spawn("t-turtle", { left: (Math.random() < .5 ? rnd(20, 32) : rnd(57, 70)) + "vw" }, under);
      t.animate([{ transform: "translateY(0)" }, { transform: "translateY(-38px)", offset: .2 }, { transform: "translateY(-38px)", offset: .8 },
        { transform: "translateY(4px)" }], { duration: 7000, easing: "ease-in-out", fill: "forwards" });
      t.querySelector(".head").animate([{ transform: "rotate(0)" }, { transform: "rotate(0)", offset: .3 }, { transform: "rotate(-14deg)", offset: .42 },
        { transform: "rotate(10deg)", offset: .58 }, { transform: "rotate(0)", offset: .7 }, { transform: "rotate(0)" }], { duration: 7000, fill: "forwards" });
      done([t], 7200);
    },
    // 4. banc de poissons : leurs ombres passent sous la surface
    school() {
      const box = document.createElement("div"); box.className = "school"; harbour.appendChild(box);   // sous les bateaux
      for (let i = 0; i < 9; i++) {
        const n = document.getElementById("t-fishshadow").content.firstElementChild.cloneNode(true);
        n.style.cssText = `position:absolute;left:${(i % 5) * 26 + (i > 4 ? 13 : 0)}px;top:${(i > 4 ? 18 : 4) + rnd(-3, 3)}px`;
        n.animate([{ transform: "translateY(0)" }, { transform: "translateY(-3px)" }], { duration: rnd(500, 800), iterations: Infinity, direction: "alternate" });
        box.appendChild(n);
      }
      box.animate([{ transform: "translate(-180px, 0)" }, { transform: "translate(35vw, 6px)", offset: .35 }, { transform: "translate(65vw, -4px)", offset: .7 },
        { transform: "translate(calc(100vw + 40px), 2px)" }], { duration: 15000, easing: "linear", fill: "forwards" });
      done([box], 15200);
    },
    // 5. régate : le « + » rattrape et double le « − »
    regatta() {
      const mk = (sym, cls) => { const n = document.getElementById("boat").content.firstElementChild.cloneNode(true);
        n.setAttribute("class", "pb regatta " + cls); n.querySelector(".sym").textContent = sym; life.appendChild(n); return n; };
      const minus = mk("−", "pb-coral"), plus = mk("+", "pb-sun");
      minus.animate([{ transform: "translateX(12vw)" }, { transform: "translateX(calc(100vw + 60px))" }], { duration: 17000, easing: "linear", fill: "forwards" });
      plus.animate([{ transform: "translateX(-60px)" }, { transform: "translateX(calc(100vw + 60px))" }], { duration: 12000, easing: "linear", fill: "forwards" });
      done([minus, plus], 17200);
    },
    // 6. pièce d'un euro : saute en tournant et retombe dans l'eau
    coin() {
      const x = rnd(18, 70), c = spawn("t-coin", { left: x + "vw" });
      const s1 = spawn("t-splash", { left: `calc(${x}vw - 3px)` }), s2 = spawn("t-splash", { left: `calc(${x}vw + 47px)`, "--dl": "1.25s" });
      c.animate([{ transform: "translate(0, 10px)", opacity: 0 }, { transform: "translate(10px, -10px)", opacity: 1, offset: .12 },
        { transform: "translate(25px, -58px)", offset: .5 }, { transform: "translate(40px, -10px)", opacity: 1, offset: .88 },
        { transform: "translate(50px, 10px)", opacity: 0 }], { duration: 1500, easing: "linear", fill: "forwards" });
      done([c, s1, s2], 2400);
    },
    // 7. plongeur du club : des bulles, puis masque et tuba qui sortent de l'eau
    diver() {
      const x = Math.random() < .5 ? rnd(22, 32) : rnd(58, 68), nodes = [];
      for (let i = 0; i < 7; i++) {
        const b = spawn("t-bubble", { left: `calc(${x}vw + ${rnd(4, 22)}px)` });
        b.animate([{ transform: "translateY(10px) scale(.6)", opacity: 0 }, { opacity: 1, offset: .2 }, { transform: `translateY(-${rnd(26, 44)}px) scale(1)`, opacity: 0 }],
          { duration: 1600, delay: i * 260 + (i > 4 ? 4200 : 0), fill: "both" });
        nodes.push(b);
      }
      const d = spawn("t-diver", { left: x + "vw" }, under);
      d.animate([{ transform: "translateY(0)" }, { transform: "translateY(0)", offset: .3 }, { transform: "translateY(-30px)", offset: .42 },
        { transform: "translateY(-30px)", offset: .74 }, { transform: "translateY(4px)", offset: .86 }, { transform: "translateY(4px)" }],
        { duration: 7600, easing: "ease-in-out", fill: "forwards" });
      nodes.push(d); done(nodes, 7800);
    },
    // 8. coucher de soleil : la mer se teinte, le soleil descend sur l'horizon
    sunset() {
      const glow = document.createElement("div"); glow.className = "sunset"; harbour.appendChild(glow);   // sous les bateaux
      const hz = document.createElement("div"); hz.className = "horizon"; life.appendChild(hz);
      const sun = spawn("t-sun", { left: rnd(55, 72) + "vw" }, hz);
      glow.animate([{ opacity: 0 }, { opacity: 1, offset: .2 }, { opacity: 1, offset: .75 }, { opacity: 0 }], { duration: 14000, fill: "forwards" });
      sun.animate([{ transform: "translateY(-4px)", opacity: 0 }, { opacity: 1, offset: .15 }, { transform: "translateY(32px)", opacity: 1, offset: .9 },
        { transform: "translateY(34px)", opacity: 0 }], { duration: 14000, easing: "ease-in", fill: "forwards" });
      done([glow, sun, hz], 14200);
    },
    // 9. mouette qui se pose sur la voile du bateau « € », puis repart
    perch() {
      const g = spawn("t-gull"), boat = document.querySelector(".harbour .b1").getBoundingClientRect();
      const gw = 46, gh = 23, tx = boat.left + boat.width / 2 - gw / 2, tb = innerHeight - boat.top - gh * .55;
      g.style.left = "0"; g.style.bottom = "0"; g.style.animation = "none";
      g.animate([{ transform: `translate(-70px, ${-(tb + 70)}px)` }, { transform: `translate(${tx * .6}px, ${-(tb + 40)}px)`, offset: .25 },
        { transform: `translate(${tx}px, ${-tb}px)`, offset: .38 }, { transform: `translate(${tx}px, ${-tb}px)`, offset: .66 },
        { transform: `translate(${tx + 160}px, ${-(tb + 60)}px)`, offset: .82 }, { transform: `translate(calc(100vw + 80px), ${-(tb + 110)}px)` }],
        { duration: 11000, easing: "ease-in-out", fill: "forwards" });
      setTimeout(() => g.classList.add("perch"), 4200);
      setTimeout(() => g.classList.remove("perch"), 7200);
      done([g], 11200);
    },
    // méduse : ondule sous la surface en montant doucement
    jellyfish() {
      const j = document.getElementById("t-jelly").content.firstElementChild.cloneNode(true);
      j.style.left = (Math.random() < .5 ? rnd(22, 34) : rnd(58, 70)) + "vw"; harbour.appendChild(j);
      j.animate([{ transform: "translateY(10px) scaleY(1)", opacity: 0 }, { opacity: .9, offset: .1 }, { transform: "translateY(-4px) scaleY(.82)", offset: .25 },
        { transform: "translateY(-14px) scaleY(1)", offset: .45 }, { transform: "translateY(-22px) scaleY(.82)", offset: .65 },
        { transform: "translateY(-30px) scaleY(1)", opacity: .9, offset: .88 }, { transform: "translateY(-34px)", opacity: 0 }], { duration: 9000, easing: "ease-in-out", fill: "forwards" });
      done([j], 9200);
    },
    // petite averse (gouttes qui tombent, ronds sur l'eau), puis un arc-en-ciel pâle au loin
    rainbow() {
      const rain = Object.assign(document.createElement("div"), { className: "rain" }), nodes = [rain];
      harbour.appendChild(rain);
      for (let i = 0; i < 70; i++) {
        const d = document.createElement("i"); d.style.left = rnd(0, 100) + "%"; rain.appendChild(d);
        d.animate([{ transform: "translate(0, 0) rotate(12deg)" }, { transform: "translate(-22px, 116px) rotate(12deg)" }],
          { duration: rnd(550, 800), delay: rnd(0, 900), iterations: 6, easing: "linear" });
      }
      for (let i = 0; i < 18; i++) {
        const r = Object.assign(document.createElement("div"), { className: "ripple" });
        r.style.left = rnd(4, 96) + "%"; r.style.bottom = rnd(6, 60) + "px"; harbour.appendChild(r); nodes.push(r);
        r.animate([{ transform: "scale(.3)", opacity: 0 }, { opacity: .8, offset: .2 }, { transform: "scale(1.6)", opacity: 0 }],
          { duration: 900, delay: rnd(300, 4200), iterations: 2, fill: "both" });
      }
      rain.animate([{ opacity: 0 }, { opacity: 1, offset: .12 }, { opacity: 1, offset: .75 }, { opacity: 0 }], { duration: 5200, fill: "forwards" });
      const bow = document.getElementById("t-rainbow").content.firstElementChild.cloneNode(true);
      bow.style.left = (Math.random() < .5 ? rnd(14, 24) : rnd(48, 58)) + "vw"; harbour.appendChild(bow); nodes.push(bow);
      bow.animate([{ opacity: 0 }, { opacity: 0, offset: .3 }, { opacity: 1, offset: .5 }, { opacity: 1, offset: .82 }, { opacity: 0 }],
        { duration: 15000, fill: "forwards" });
      done(nodes, 15200);
    },
    // fou de Bassan : passe, replie ses ailes et plonge en piqué
    gannet() {
      const x = rnd(20, 60), g = spawn("t-gannet", { left: x + "vw" }), sp = spawn("t-splash", { left: `calc(${x}vw + 150px)`, "--dl": "3.1s" });
      g.animate([{ transform: "translate(-160px, 0) rotate(0)" }, { transform: "translate(80px, -6px) rotate(0)", offset: .5 },
        { transform: "translate(120px, 0) rotate(60deg)", offset: .62 }, { transform: "translate(150px, 70px) rotate(80deg) scaleX(.6)", opacity: 1, offset: .9 },
        { transform: "translate(152px, 78px) rotate(80deg) scaleX(.6)", opacity: 0 }], { duration: 3500, easing: "ease-in", fill: "forwards" });
      done([g, sp], 4400);
    },
    // kayakiste : traverse en pagayant
    kayak() { const k = spawn("t-kayak");
      k.animate([{ transform: "translateX(-80px)" }, { transform: "translateX(calc(100vw + 80px))" }], { duration: 24000, easing: "linear", fill: "forwards" });
      done([k], 24200); },
    // voilier au loin, sur l'horizon
    sailboat() { const v = spawn("t-sailboat");
      v.animate([{ transform: "translateX(-60px)", opacity: 0 }, { opacity: 1, offset: .08 }, { opacity: 1, offset: .92 },
        { transform: "translateX(calc(100vw + 60px))", opacity: 0 }], { duration: 30000, easing: "linear", fill: "forwards" });
      done([v], 30200); },
    // pieuvre : sort la tête, ses bras sortent de l'eau autour d'elle et ondulent (un fait coucou), replonge
    octopus() {
      const o = spawn("t-octopus", { left: (Math.random() < .5 ? rnd(20, 30) : rnd(56, 66)) + "vw" }, under);
      o.animate([{ transform: "translateY(0)" }, { transform: "translateY(-38px)", offset: .2 }, { transform: "translateY(-38px)", offset: .8 },
        { transform: "translateY(4px)" }], { duration: 7000, easing: "ease-in-out", fill: "forwards" });
      o.querySelectorAll(".arm").forEach((a, i) => {
        const wave = i === 1 ? [0, -34, 18, -34, 18, 0] : [0, 10, -8, 10, -8, 0];
        a.animate(wave.map((d, k) => ({ transform: `rotate(${d}deg)`, offset: k ? .25 + k * .1 : 0 })).concat([{ transform: "rotate(0)", offset: 1 }]),
          { duration: 7000, delay: i * 120, fill: "both" });
      });
      done([o], 7200);
    },
    // canard en plastique : dérive tranquillement
    duck() { const d = spawn("t-duck");
      d.animate([{ transform: "translateX(-40px)" }, { transform: "translateX(calc(100vw + 40px))" }], { duration: 26000, easing: "linear", fill: "forwards" });
      done([d], 26200); },
    // étoile filante (la nuit)
    star() { const s = spawn("t-star", { left: rnd(10, 50) + "vw" });
      s.animate([{ transform: "translate(0, 0)", opacity: 0 }, { opacity: 1, offset: .12 }, { opacity: 1, offset: .7 },
        { transform: "translate(320px, 60px)", opacity: 0 }], { duration: 2400, easing: "ease-out", fill: "forwards" });
      done([s], 2600); },
    // aileron de requin : tourne en rond… puis s'en va
    shark() { const x = rnd(25, 60), f = spawn("t-fin", { left: x + "vw" }), k = [];
      for (let i = 0; i <= 24; i++) { const a = i / 24 * Math.PI * 3;
        k.push({ transform: `translate(${Math.sin(a) * 70}px, ${-Math.cos(a) * 10}px) scaleX(${Math.cos(a) >= 0 ? 1 : -1})`, offset: i / 24 * .8 }); }
      k.push({ transform: "translate(calc(100vw), 0)", offset: 1 });
      k[0].opacity = 0; k[1].opacity = 1;
      f.animate(k, { duration: 14000, easing: "linear", fill: "forwards" });
      done([f], 14200); },

    // 10. tout est réglé : un cachalot souffle des confettis (très rare dans l'app)
    party() {
      const x = Math.random() < .5 ? rnd(20, 30) : rnd(57, 66), w = spawn("t-whale", { left: x + "vw" }, under), nodes = [w];
      w.querySelector(".spout").style.display = "none";
      const colors = ["var(--teal)", "var(--pbs-hl)", "var(--pbc-hl)", "var(--pbb-hl)", "#fff"];
      setTimeout(() => {
        for (let i = 0; i < 28; i++) {
          const c = document.createElement("div"); c.className = "confetti"; c.style.background = colors[i % 5];
          c.style.left = `calc(${x}vw + 46px)`; c.style.bottom = "112px"; life.appendChild(c); nodes.push(c);
          const vx = rnd(-70, 70), up = rnd(60, 110), rot = rnd(-540, 540);
          c.animate([{ transform: "translate(0,0) rotate(0)", opacity: 1 }, { transform: `translate(${vx * .6}px, ${-up}px) rotate(${rot / 2}deg)`, opacity: 1, offset: .4 },
            { transform: `translate(${vx}px, ${-up + 50}px) rotate(${rot}deg)`, opacity: 1, offset: .75 },
            { transform: `translate(${vx * 1.2}px, ${-up + 100}px) rotate(${rot * 1.3}deg)`, opacity: 0 }],
            { duration: rnd(2200, 3000), easing: "ease-out", fill: "forwards" });
        }
      }, 2600);
      done(nodes, 9300);
    },
  };

  const reduced = matchMedia("(prefers-reduced-motion: reduce)");
  const store = {
    get(k, d) { try { const v = localStorage.getItem(k); return v === null ? d : JSON.parse(v); } catch (e) { return d; } },
    set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) { /* stockage indisponible : tant pis */ } },
  };
  function dark() {
    const t = document.documentElement.dataset.theme;
    return t === "dark" || (t !== "light" && matchMedia("(prefers-color-scheme: dark)").matches);
  }
  function quietNow() {   // un formulaire de l'accueil ouvert, la fenêtre cachée, ou animations réduites
    return reduced.matches || document.hidden ||
      Array.from(document.querySelectorAll(".home-panel")).some((p) => !p.hidden);
  }
  // surprises qui traversent : tantôt de gauche à droite, tantôt de droite à gauche (calque retourné en miroir)
  const CROSSING = ["gull", "bottle", "school", "regatta", "flyingfish", "dolphins", "coin",
    "kayak", "sailboat", "duck", "shark", "gannet", "star"];
  const FACING = ["whale", "turtle", "octopus"];             // sur place : regardent d'un côté ou de l'autre
  function play(name, mirror) {
    if (busy || !EGGS[name]) return false;
    busy = true;
    document.querySelectorAll(".sea-mirror:empty").forEach((n) => n.remove());
    if (mirror === undefined) mirror = Math.random() < .5;
    const flip = mirror && CROSSING.includes(name);
    if (flip) {
      life = LIFE.appendChild(Object.assign(document.createElement("div"), { className: "sea-mirror" }));
      harbour = HARBOUR.appendChild(Object.assign(document.createElement("div"), { className: "sea-mirror" }));
    }
    const before = new Set(under.children);
    try { EGGS[name](); } catch (e) { busy = false; } finally { life = LIFE; harbour = HARBOUR; }
    if (mirror && FACING.includes(name)) Array.from(under.children).filter((n) => !before.has(n)).forEach((n) => n.classList.add("faces-left"));
    if (!busy) return false;
    const seen = store.get("tc-mer-vues", []).filter((n) => n !== name);
    seen.unshift(name); store.set("tc-mer-vues", seen.slice(0, 4));
    return true;
  }
  function choose() {
    const h = new Date().getHours(), recent = store.get("tc-mer-vues", []);
    const pool = [];
    const add = (n, w) => { if (recent[0] !== n) for (let i = 0; i < w; i++) pool.push(n); };
    ["whale", "dolphins", "gull", "bottle", "flyingfish", "turtle", "school", "regatta", "coin", "diver",
     "jellyfish", "gannet", "kayak", "sailboat", "octopus", "duck", "shark"]
      .forEach((n) => add(n, recent.slice(0, 3).includes(n) ? 1 : 3));
    if (!dark()) add("rainbow", 2);                    // de jour
    if (dark()) add("star", 3);                        // la nuit
    add("perch", 1);                                   // plus rare
    if (h >= 18 && h < 23) add("sunset", 3);           // le soir
    if (dark()) add("lighthouse", 3);                  // la nuit (thème sombre)
    return pool[Math.floor(Math.random() * pool.length)];
  }
  function party() {   // tous les voyages réglés : une fois, à la première visite qui suit
    const settled = life.dataset.regle === "1";
    if (!settled) { store.set("tc-mer-fete", false); return false; }
    if (store.get("tc-mer-fete", false)) return false;
    store.set("tc-mer-fete", true);
    return true;
  }
  let next = null;
  function schedule(ms) { clearTimeout(next); next = setTimeout(tick, ms); }
  function tick() {
    if (!quietNow()) play(choose());
    schedule(rnd(40000, 70000));
  }
  // essais : window.tcMer.play("whale") (aucune règle ni hasard)
  window.tcMer = { play: (n, mirror) => { busy = false; return play(n, !!mirror); }, names: () => Object.keys(EGGS) };
  if (reduced.matches) return;
  setTimeout(() => {
    if (quietNow()) return schedule(rnd(40000, 70000));
    if (party()) play("party");
    else if (Math.random() < .5) play(choose());
    schedule(rnd(40000, 70000));
  }, rnd(4000, 8000));
})();
