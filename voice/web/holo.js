/* J.A.R.V.I.S. — modalità olografica.

   Entrando in modalità ologramma, le schede VERE della HUD (diagnostica,
   reattore, registro, barra, controlli) vengono "sollevate" in uno spazio 3D e
   diventano manipolabili con la mano in aria:
     punta dell'indice  -> cursore
     pinch (dita unite)  -> afferra una scheda e trascinala, o clicca un tasto

   Non creiamo schede finte: prendiamo quelle reali, le spostiamo dentro la
   scena e alla chiusura le rimettiamo esattamente dov'erano. Così restano vive
   (CPU, orologio, reattore continuano ad aggiornarsi mentre fluttuano) e non c'è
   nulla da tenere sincronizzato in doppio.

   Il tracciamento viene da gestures.js in "modalità puntatore"; c'è anche un
   ripiego a MOUSE (tasto premuto = pinch), comodo come alternativa e per
   collaudare senza webcam. Non è un ologramma vero nell'aria — per quello serve
   un visore — ma l'effetto "sposto lastre di luce con le mani" è tutto qui. */
window.JarvisHolo = (() => {
  "use strict";

  /* Le schede reali da sollevare, con la loro "profondità" (translateZ): più
     alta = più in primo piano. Il reattore sta davanti a tutto. */
  const CARTE = [
    { sel: "header.topbar", z: 30 },
    { sel: "aside.panel-left", z: 70 },
    { sel: "main.stage", z: 120 },
    { sel: "aside.panel-right", z: 70 },
    { sel: "footer.controls", z: 45 },
  ];
  const PERSP = 1600;           // deve combaciare con `perspective` di #holo nel CSS

  let root = null, scene = null, cursor = null;
  let built = false, aperto = false;
  let onClose = null;
  let sollevate = [];           // {el, parent, next, style} per rimettere tutto a posto

  /* Trascinamento per differenza in coordinate schermo; la parallasse è
     congelata durante la presa così l'angolo resta costante e la scheda segue la
     mano. Il fattore prospettico (dragScala) compensa il fatto che una scheda in
     primo piano si muove sullo schermo più del suo left/top. */
  let dragging = null, startLeft = 0, startTop = 0, startPx = 0, startPy = 0;
  let dragScala = 1;
  let pinchPrev = false;
  let zTop = 20;
  let fadeTimer = null;

  function build() {
    if (built) return;
    root = document.createElement("div");
    root.id = "holo";
    root.innerHTML =
      `<div id="holo-scene"></div>` +
      `<div class="holo-btn" id="holo-esci" data-act="esci">ESCI</div>` +
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

  /* Solleva le schede reali: le fissa dove sono ora, le rende schede olografiche
     e le sposta dentro la scena. Ricorda com'erano per la chiusura. */
  function solleva() {
    sollevate = [];
    for (const c of CARTE) {
      const el = document.querySelector(c.sel);
      if (!el) continue;
      const r = el.getBoundingClientRect();
      sollevate.push({ el, parent: el.parentNode, next: el.nextSibling,
                       style: el.getAttribute("style") });
      el.classList.add("holo-card");
      el.style.position = "absolute";
      el.style.left = Math.round(r.left) + "px";
      el.style.top = Math.round(r.top) + "px";
      el.style.width = Math.round(r.width) + "px";
      el.style.height = Math.round(r.height) + "px";
      el.style.margin = "0";
      el.style.setProperty("--z", c.z + "px");
      scene.appendChild(el);        // reparent: mantiene listener, canvas e contenuti vivi
    }
  }

  /* Rimette ogni scheda dove stava, con lo stile originale. In ordine INVERSO:
     il "next" di ciascuna è la scheda seguente, quindi ripristinando prima le
     ultime i riferimenti restano validi e la griglia torna identica. */
  function riposa() {
    for (let i = sollevate.length - 1; i >= 0; i--) {
      const s = sollevate[i];
      s.el.classList.remove("holo-card");
      // Ripristina lo stile originale se c'era; se era assente O vuoto togliamo
      // del tutto l'attributo, così non resta un `style=""` residuo.
      if (s.style) s.el.setAttribute("style", s.style);
      else s.el.removeAttribute("style");
      s.parent.insertBefore(s.el, s.next);
    }
    sollevate = [];
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
    // trascini, per non falsare la corrispondenza schermo↔scheda.
    if (!dragging) {
      const rx = py / window.innerHeight - 0.5;
      const ry = px / window.innerWidth - 0.5;
      scene.style.transform =
        `rotateX(${(-rx * 5).toFixed(2)}deg) rotateY(${(ry * 6).toFixed(2)}deg)`;
    }

    if (pinch && !pinchPrev) pinchGiu(px, py);
    else if (!pinch && pinchPrev) pinchSu();

    if (pinch && dragging) {
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
    const card = el.closest(".holo-card");
    if (card) {
      dragging = card;
      startLeft = parseFloat(card.style.left) || 0;
      startTop = parseFloat(card.style.top) || 0;
      startPx = px; startPy = py;
      const z = parseFloat(getComputedStyle(card).getPropertyValue("--z")) || 0;
      dragScala = PERSP / (PERSP - z);       // >1 per le schede in primo piano
      card.style.zIndex = ++zTop;
      card.classList.add("dragging");
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

  function suTasto(e) { if (e.key === "Escape") uscita(); }

  /* ---------- ciclo di vita ---------- */
  function apri(opts) {
    if (aperto) return;
    build();
    onClose = (opts && opts.onClose) || null;
    solleva();                    // le schede reali entrano nello spazio 3D
    root.style.display = "block";
    aperto = true;
    scene.style.transform = "";
    pinchPrev = false; mouseGiu = false; dragging = null; zTop = 20;
    cursor.classList.add("lost");
    document.addEventListener("keydown", suTasto);
  }

  function spegni() {
    if (!aperto) return;
    aperto = false;
    dragging = null; pinchPrev = false;
    clearTimeout(fadeTimer);
    document.removeEventListener("keydown", suTasto);
    riposa();                     // le schede tornano nella griglia, com'erano
    root.style.display = "none";
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
