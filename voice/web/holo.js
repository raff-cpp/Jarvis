/* J.A.R.V.I.S. — modalità olografica.

   Un livello di pannelli traslucidi che "fluttuano" davanti alla HUD, con
   profondità e parallasse, che si manipolano con la mano in aria:
     punta dell'indice  -> cursore
     pinch (dita unite)  -> afferra un pannello e trascinalo, o clicca un tasto

   Il tracciamento viene da gestures.js in "modalità puntatore"; qui ci occupiamo
   solo di scena e interazione. C'è anche un ripiego a MOUSE: comodo come
   alternativa e indispensabile per collaudare senza webcam (mouse premuto =
   pinch). Nessun ologramma vero nell'aria — per quello serve un visore — ma
   l'effetto scenico "sposto lastre di luce con le mani" è tutto qui. */
window.JarvisHolo = (() => {
  "use strict";

  let root = null, scene = null, cursor = null;
  let built = false, aperto = false;
  let onClose = null;

  /* Trascinamento: registriamo la posizione del pannello e del cursore all'atto
     della presa, poi spostiamo per DIFFERENZA in coordinate schermo. Durante la
     presa la parallasse viene congelata, così l'angolo resta costante e lo
     scostamento schermo↔pannello coincide (a meno di <2% per angoli piccoli):
     niente inversione di matrici 3D, e il pannello segue la mano fedelmente. */
  let dragging = null, startLeft = 0, startTop = 0, startPx = 0, startPy = 0;
  let dragScala = 1;            // vedi pinchGiu: compensa la prospettiva del pannello
  let pinchPrev = false;
  let zTop = 20;                 // z-index crescente: l'ultimo preso va davanti
  let statsTimer = null, clockTimer = null, fadeTimer = null;

  const PERSP = 1400;           // deve combaciare con `perspective` di #holo nel CSS

  const PANNELLI = `
    <div class="holo-panel" data-panel style="--z:40px; left:7%; top:20%">
      <div class="holo-grip"></div>
      <h3>SISTEMA</h3>
      <div class="holo-stat"><label>CPU</label><div class="holo-bar"><i id="holo-cpu"></i></div><span id="holo-cpu-v">--%</span></div>
      <div class="holo-stat"><label>MEMORIA</label><div class="holo-bar"><i id="holo-ram"></i></div><span id="holo-ram-v">-- / -- GB</span></div>
      <div class="holo-stat"><label>DISCO C:</label><div class="holo-bar"><i id="holo-disk"></i></div><span id="holo-disk-v">-- GB</span></div>
    </div>
    <div class="holo-panel" data-panel style="--z:96px; left:58%; top:16%">
      <div class="holo-grip"></div>
      <h3>OROLOGIO</h3>
      <div class="holo-clock" id="holo-time">--:--:--</div>
      <div class="holo-date" id="holo-date">--</div>
    </div>
    <div class="holo-panel holo-brand" data-panel style="--z:150px; left:33%; top:52%">
      <div class="holo-grip"></div>
      <div class="holo-core"><span></span></div>
      <div class="holo-mark">J.A.R.V.I.S.</div>
      <div class="holo-tag">Just A Rather Very Intelligent System</div>
    </div>
    <div class="holo-btn" data-act="esci" style="--z:170px; right:6%; top:9%">ESCI</div>
  `;

  function build() {
    if (built) return;
    root = document.createElement("div");
    root.id = "holo";
    root.innerHTML =
      `<div id="holo-scene">${PANNELLI}</div>` +
      `<div id="holo-cursor"></div>` +
      `<div id="holo-hint">INDICE = PUNTATORE · PINCH = AFFERRA / CLIC ·` +
      ` <b>ESC</b> o «ESCI» per uscire</div>`;
    document.body.appendChild(root);
    scene = root.querySelector("#holo-scene");
    cursor = root.querySelector("#holo-cursor");
    // Ripiego a mouse (e banco di prova): il tasto premuto vale come pinch.
    root.addEventListener("mousemove", suMouse);
    root.addEventListener("mousedown", suMouse);
    root.addEventListener("mouseup", suMouse);
    built = true;
  }

  /* Converte le posizioni iniziali da percentuale a pixel, così il
     trascinamento lavora su numeri semplici. Si calcola dalla percentuale
     GREZZA, non da getBoundingClientRect: quest'ultimo restituisce la posizione
     già proiettata dal translateZ, e riscriverla come left/top farebbe
     ri-applicare la prospettiva una seconda volta (il pannello "scivolerebbe"
     alla prima apertura). Convertiamo una volta sola: se è già in px (pannello
     spostato in una sessione precedente) lo lasciamo dov'è. */
  function disponi() {
    const w = window.innerWidth, h = window.innerHeight;
    scene.querySelectorAll(".holo-panel").forEach((p) => {
      if (p.style.left.endsWith("%")) p.style.left = Math.round(parseFloat(p.style.left) / 100 * w) + "px";
      if (p.style.top.endsWith("%")) p.style.top = Math.round(parseFloat(p.style.top) / 100 * h) + "px";
    });
  }

  /* ---------- interazione, in coordinate schermo (px) ---------- */
  function suPuntatore(px, py, pinch) {
    if (!aperto) return;
    cursor.style.left = px + "px";
    cursor.style.top = py + "px";
    cursor.classList.toggle("grab", pinch);
    cursor.classList.remove("lost");
    clearTimeout(fadeTimer);
    fadeTimer = setTimeout(() => cursor.classList.add("lost"), 500);

    // Parallasse: la scena si inclina seguendo il cursore. Congelata mentre
    // trascini, per non falsare la corrispondenza schermo↔pannello.
    if (!dragging) {
      const rx = py / window.innerHeight - 0.5;
      const ry = px / window.innerWidth - 0.5;
      scene.style.transform =
        `rotateX(${(-rx * 7).toFixed(2)}deg) rotateY(${(ry * 9).toFixed(2)}deg)`;
    }

    if (pinch && !pinchPrev) pinchGiu(px, py);
    else if (!pinch && pinchPrev) pinchSu();

    if (pinch && dragging) {
      // Diviso per la scala prospettica: un pannello "vicino" (translateZ alto)
      // sullo schermo si muove più del suo left/top, quindi lo spostamento in
      // left/top va ridotto d'altrettanto perché segua il cursore 1:1.
      dragging.style.left = startLeft + (px - startPx) / dragScala + "px";
      dragging.style.top = startTop + (py - startPy) / dragScala + "px";
    }
    pinchPrev = pinch;
  }

  function pinchGiu(px, py) {
    const el = document.elementFromPoint(px, py);
    if (!el) return;
    const btn = el.closest(".holo-btn");
    if (btn) { azione(btn.dataset.act); lampeggia(); return; }
    const panel = el.closest(".holo-panel");
    if (panel) {
      dragging = panel;
      startLeft = parseFloat(panel.style.left) || 0;
      startTop = parseFloat(panel.style.top) || 0;
      startPx = px; startPy = py;
      const z = parseFloat(getComputedStyle(panel).getPropertyValue("--z")) || 0;
      dragScala = PERSP / (PERSP - z);       // >1 per i pannelli in primo piano
      panel.style.zIndex = ++zTop;
      panel.classList.add("dragging");
    }
  }

  function pinchSu() {
    if (dragging) { dragging.classList.remove("dragging"); dragging = null; }
  }

  function azione(act) {
    if (act === "esci") uscita();
  }

  function lampeggia() {
    cursor.classList.add("click");
    setTimeout(() => cursor.classList.remove("click"), 180);
  }

  /* ---------- mouse (ripiego / collaudo) ---------- */
  let mouseGiu = false;
  function suMouse(e) {
    if (e.type === "mousedown") mouseGiu = true;
    else if (e.type === "mouseup") mouseGiu = false;
    suPuntatore(e.clientX, e.clientY, mouseGiu);
  }

  /* ---------- dati dei pannelli ---------- */
  function pct(v) { return Math.min(100, Math.max(0, v)) + "%"; }

  async function aggiornaStats() {
    try {
      const s = await (await fetch("/api/stats")).json();
      document.getElementById("holo-cpu").style.width = pct(s.cpu);
      document.getElementById("holo-cpu-v").textContent = s.cpu + "%";
      const ramPct = s.ramTotal ? (s.ramUsed / s.ramTotal) * 100 : 0;
      document.getElementById("holo-ram").style.width = pct(ramPct);
      document.getElementById("holo-ram-v").textContent = `${s.ramUsed} / ${s.ramTotal} GB`;
      const diskPct = s.diskTotal ? ((s.diskTotal - s.diskFree) / s.diskTotal) * 100 : 0;
      document.getElementById("holo-disk").style.width = pct(diskPct);
      document.getElementById("holo-disk-v").textContent = `${s.diskFree} GB liberi`;
    } catch (e) { /* il server può essere momentaneamente occupato */ }
  }

  const GIORNI = ["DOM", "LUN", "MAR", "MER", "GIO", "VEN", "SAB"];
  const MESI = ["GEN", "FEB", "MAR", "APR", "MAG", "GIU", "LUG", "AGO", "SET", "OTT", "NOV", "DIC"];
  function aggiornaOrologio() {
    const d = new Date();
    document.getElementById("holo-time").textContent = d.toLocaleTimeString("it-IT");
    document.getElementById("holo-date").textContent =
      `${GIORNI[d.getDay()]} ${d.getDate()} ${MESI[d.getMonth()]} ${d.getFullYear()}`;
  }

  function suTasto(e) { if (e.key === "Escape") uscita(); }

  /* ---------- ciclo di vita ---------- */
  function apri(opts) {
    build();
    onClose = (opts && opts.onClose) || null;
    root.style.display = "block";
    aperto = true;
    disponi();
    scene.style.transform = "";
    pinchPrev = false; mouseGiu = false; dragging = null;
    cursor.classList.add("lost");
    aggiornaStats(); aggiornaOrologio();
    statsTimer = setInterval(aggiornaStats, 4000);
    clockTimer = setInterval(aggiornaOrologio, 1000);
    document.addEventListener("keydown", suTasto);
  }

  function spegni() {
    if (!aperto) return;
    aperto = false;
    root.style.display = "none";
    clearInterval(statsTimer); clearInterval(clockTimer); clearTimeout(fadeTimer);
    document.removeEventListener("keydown", suTasto);
    dragging = null; pinchPrev = false;
  }

  // Chiusura richiesta dall'interno (tasto ESCI o ESC): avvisa chi ci ha aperti,
  // così la casella "OLOGRAMMA" si deseleziona e il puntatore si spegne.
  function uscita() {
    spegni();
    if (onClose) onClose();
  }

  return {
    open: apri,
    close: spegni,               // chiusura dall'esterno: non richiama onClose
    pointer: (p) => suPuntatore(p.x * window.innerWidth, p.y * window.innerHeight, p.pinch),
    isOpen: () => aperto,
  };
})();
