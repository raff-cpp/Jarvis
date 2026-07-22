/* J.A.R.V.I.S. — controllo a gesti con la webcam (MediaPipe Hands).

   Filosofia: pochi gesti, molto affidabili. Le mani si muovono in continuazione
   mentre si parla o si lavora, quindi NIENTE deve scattare finché non si arma
   esplicitamente il sistema con il palmo aperto. Meglio un gesto che va rifatto
   che una traccia saltata mentre gesticoli al telefono.

   Gesti previsti:
     palmo aperto tenuto fermo   -> arma i comandi
     pinch pollice-indice        -> volume (continuo, come una manopola)
     mano aperta, swipe L/R      -> traccia precedente / successiva
     pugno chiuso, swipe L/R     -> sposta la finestra sull'altro schermo
*/
(() => {
  "use strict";

  const CDN = "https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@0.10.35";
  const MODEL = "https://storage.googleapis.com/mediapipe-models/hand_landmarker" +
                "/hand_landmarker/float16/1/hand_landmarker.task";

  /* --- Soglie: tarate per stare larghi sui falsi positivi --- */
  const ARM_HOLD_MS = 600;      // quanto tenere il palmo aperto per armare
  const DISARM_MS = 4000;       // inattività dopo cui si disarma da solo
  const PINCH_MIN = 0.10;       // distanza pollice-indice = volume 0
  const PINCH_MAX = 0.65;       // distanza pollice-indice = volume 100
  const SMOOTH = 0.35;          // filtro sul tremolio (0 = fermo, 1 = nessun filtro)
  const VOL_MIN_DELTA = 2;      // non inviare variazioni impercettibili
  const VOL_MIN_MS = 90;        // né più di ~11 aggiornamenti al secondo
  const SWIPE_DIST = 0.17;      // ampiezza minima di uno swipe (frazione di schermo)
  const SWIPE_MS = 420;         // finestra temporale dello swipe
  const SWIPE_COOLDOWN = 900;   // pausa fra due gesti discreti

  let landmarker = null;
  let stream = null;
  let running = false;
  let video = null;
  let overlay = null;
  let octx = null;
  let onEvent = () => {};
  let onState = () => {};

  /* --- Stato del riconoscitore --- */
  let armed = false;
  let openSince = 0;
  let lastSeen = 0;
  let lastAction = 0;
  let smoothPinch = null;
  let lastVolSent = -1;
  let lastVolAt = 0;
  let track = [];               // storico posizione polso: {x, t, pose}
  let pinching = false;

  /* ---------- geometria della mano ---------- */
  const dist = (a, b) => Math.hypot(a.x - b.x, a.y - b.y);

  /** Scala della mano: serve a rendere le distanze indipendenti da quanto
      sei lontano dalla webcam. */
  function handScale(lm) {
    return Math.max(dist(lm[0], lm[9]), 1e-6);
  }

  /** Un dito è teso se la punta è più lontana dal polso della nocca centrale. */
  function fingerOut(lm, tip, pip) {
    return dist(lm[0], lm[tip]) > dist(lm[0], lm[pip]) * 1.12;
  }

  function pose(lm) {
    const fingers = [fingerOut(lm, 8, 6), fingerOut(lm, 12, 10),
                     fingerOut(lm, 16, 14), fingerOut(lm, 20, 18)];
    const out = fingers.filter(Boolean).length;
    const pinchDist = dist(lm[4], lm[8]) / handScale(lm);

    // Pinch: pollice e indice vicini, medio/anulare/mignolo ripiegati.
    // Il controllo sulle altre dita evita che una mano aperta a riposo,
    // con pollice e indice casualmente vicini, diventi un pinch.
    const closedOthers = !fingers[1] && !fingers[2];
    if (pinchDist < 0.32 && closedOthers) return { kind: "pinch", pinchDist };
    if (out === 4) return { kind: "open", pinchDist };
    if (out === 0) return { kind: "fist", pinchDist };
    return { kind: "other", pinchDist };
  }

  /* ---------- disegno del riscontro visivo ---------- */
  const BONES = [[0,1],[1,2],[2,3],[3,4],[0,5],[5,6],[6,7],[7,8],[5,9],[9,10],
                 [10,11],[11,12],[9,13],[13,14],[14,15],[15,16],[13,17],[17,18],
                 [18,19],[19,20],[0,17]];

  function draw(lm, kind) {
    if (!octx) return;
    const w = overlay.width, h = overlay.height;
    octx.clearRect(0, 0, w, h);
    if (!lm) return;
    const colore = armed ? (getComputedStyle(document.body)
                            .getPropertyValue("--glow").trim() || "#35e0ff")
                         : "rgba(150,170,180,.55)";
    octx.strokeStyle = colore;
    octx.fillStyle = colore;
    octx.lineWidth = 2;
    // L'immagine è specchiata: rispecchiamo anche i punti, così il tuo
    // movimento a destra appare a destra.
    const X = (p) => (1 - p.x) * w, Y = (p) => p.y * h;
    for (const [a, b] of BONES) {
      octx.beginPath();
      octx.moveTo(X(lm[a]), Y(lm[a]));
      octx.lineTo(X(lm[b]), Y(lm[b]));
      octx.stroke();
    }
    for (const p of lm) {
      octx.beginPath();
      octx.arc(X(p), Y(p), 3, 0, Math.PI * 2);
      octx.fill();
    }
    if (kind && armed) {
      octx.font = "11px Consolas, monospace";
      octx.fillText(kind.toUpperCase(), 8, 16);
    }
  }

  /* ---------- riconoscimento ---------- */
  function classify(lm, now) {
    const p = pose(lm);
    lastSeen = now;

    // --- armamento con il palmo aperto tenuto fermo ---
    if (!armed) {
      if (p.kind === "open") {
        if (!openSince) openSince = now;
        if (now - openSince >= ARM_HOLD_MS) {
          armed = true;
          openSince = 0;
          track = [];
          onState({ armed: true });
        }
      } else {
        openSince = 0;
      }
      return p.kind;
    }

    // --- pinch: volume continuo ---
    if (p.kind === "pinch") {
      pinching = true;
      const grezzo = Math.min(1, Math.max(0,
        (p.pinchDist - PINCH_MIN) / (PINCH_MAX - PINCH_MIN)));
      smoothPinch = smoothPinch === null
        ? grezzo : smoothPinch + (grezzo - smoothPinch) * SMOOTH;
      const vol = Math.round(smoothPinch * 100);
      if (Math.abs(vol - lastVolSent) >= VOL_MIN_DELTA && now - lastVolAt >= VOL_MIN_MS) {
        lastVolSent = vol;
        lastVolAt = now;
        onEvent({ action: "volume", value: vol });
        onState({ armed: true, hint: `volume ${vol}%` });
      }
      track = [];
      return "pinch";
    }
    if (pinching) { pinching = false; smoothPinch = null; }

    // --- swipe: serve un movimento ampio e coerente nella stessa posa ---
    track.push({ x: lm[0].x, t: now, kind: p.kind });
    while (track.length && now - track[0].t > SWIPE_MS) track.shift();

    if ((p.kind === "open" || p.kind === "fist") && track.length >= 4
        && now - lastAction > SWIPE_COOLDOWN) {
      const inizio = track[0];
      const spostamento = lm[0].x - inizio.x;
      const stessaPosa = track.every((s) => s.kind === p.kind);
      if (stessaPosa && Math.abs(spostamento) >= SWIPE_DIST) {
        // La webcam specchia: uno spostamento a destra per te è -x qui.
        const verso = spostamento < 0 ? "right" : "left";
        lastAction = now;
        track = [];
        if (p.kind === "open") {
          const v = verso === "right" ? "next" : "previous";
          onEvent({ action: "media", value: v });
          onState({ armed: true, hint: v === "next" ? "traccia avanti" : "traccia indietro" });
        } else {
          onEvent({ action: "window", value: verso });
          onState({ armed: true, hint: `finestra a ${verso === "right" ? "destra" : "sinistra"}` });
        }
      }
    }
    return p.kind;
  }

  /* ---------- ciclo principale ---------- */
  function loop() {
    if (!running) return;
    const now = performance.now();
    let kind = null;
    let lm = null;
    if (video.readyState >= 2) {
      try {
        const res = landmarker.detectForVideo(video, now);
        if (res && res.landmarks && res.landmarks.length) {
          lm = res.landmarks[0];
          kind = classify(lm, now);
        }
      } catch (e) { /* fotogramma saltato: non è un problema */ }
    }
    // Disarmo automatico: se la mano sparisce, i comandi si spengono.
    if (armed && now - lastSeen > DISARM_MS) {
      armed = false;
      smoothPinch = null;
      lastVolSent = -1;
      track = [];
      onState({ armed: false });
    }
    draw(lm, kind);
    requestAnimationFrame(loop);
  }

  /* ---------- API pubblica ---------- */
  async function start(opts) {
    onEvent = opts.onEvent || onEvent;
    onState = opts.onState || onState;
    video = opts.video;
    overlay = opts.overlay;
    octx = overlay ? overlay.getContext("2d") : null;

    if (!landmarker) {
      const vision = await import(CDN + "/vision_bundle.mjs");
      const fileset = await vision.FilesetResolver.forVisionTasks(CDN + "/wasm");
      landmarker = await vision.HandLandmarker.createFromOptions(fileset, {
        baseOptions: { modelAssetPath: MODEL, delegate: "GPU" },
        runningMode: "VIDEO",
        numHands: 1,
      });
    }

    stream = await navigator.mediaDevices.getUserMedia({
      video: { width: 640, height: 480, facingMode: "user" },
    });
    video.srcObject = stream;
    await video.play();
    running = true;
    armed = false;
    lastSeen = performance.now();
    requestAnimationFrame(loop);
  }

  function stop() {
    running = false;
    armed = false;
    smoothPinch = null;
    lastVolSent = -1;
    track = [];
    if (stream) {
      stream.getTracks().forEach((t) => t.stop());   // spegne la spia della webcam
      stream = null;
    }
    if (video) video.srcObject = null;
    if (octx) octx.clearRect(0, 0, overlay.width, overlay.height);
  }

  window.JarvisGestures = { start, stop, isRunning: () => running };
})();
