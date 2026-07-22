/* J.A.R.V.I.S. — controllo a gesti con la webcam (MediaPipe Hands).

   Filosofia: pochi gesti, molto affidabili. Le mani si muovono in continuazione
   mentre si parla o si lavora, quindi NIENTE deve scattare finché non si arma
   esplicitamente il sistema con il palmo aperto. Meglio un gesto che va rifatto
   che una traccia saltata mentre gesticoli al telefono.

   Gesti previsti:
     palmo aperto tenuto fermo   -> arma i comandi
     pinch pollice-indice        -> volume (continuo, come una manopola)
     mano aperta, swipe L/R      -> traccia precedente / successiva
     mano aperta che si chiude   -> finestra sull'altro schermo
*/
(() => {
  "use strict";

  const CDN = "https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@0.10.35";
  const MODEL = "https://storage.googleapis.com/mediapipe-models/hand_landmarker" +
                "/hand_landmarker/float16/1/hand_landmarker.task";
  const MODEL_VOLTO = "https://storage.googleapis.com/mediapipe-models/face_detector" +
                      "/blaze_face_short_range/float16/1/blaze_face_short_range.tflite";

  /* --- Presenza ---
     Riconoscere che ti sei seduto o che te ne sei andato. Le soglie sono
     volutamente LARGHE: bloccare lo schermo per sbaglio mentre leggi un foglio
     fuori inquadratura sarebbe molto più fastidioso di un blocco mancato. */
  const VOLTO_OGNI_MS = 500;      // il volto non serve a 30 fps: basta ogni mezzo secondo
  const ARRIVO_CONFERMA_MS = 1500; // quanto devi restare inquadrato per contare come "arrivato"

  /* Due soglie DISTINTE, ed e' il punto piu' importante di tutta la presenza.
     Salutare e bloccare il PC non sono la stessa decisione e non possono
     condividere lo stesso timer: legarli voleva dire che, portando il blocco a
     un'ora, ti alzavi e tornavi e non succedeva piu' niente per un'ora.
     Il saluto puo' permettersi di sbagliare (una frase di troppo), il blocco
     no: da qui i due ordini di grandezza di differenza. */
  const ASSENZA_SALUTO_MS = 60000;      // 1 minuto: al ritorno ti risaluta
  const USCITA_PREAVVISO_MS = 3540000;  // 59 minuti: avvisa che sta per bloccare
  const USCITA_MS = 3600000;            // un'ora: solo ora blocca la postazione

  /* Un'ora di attesa non e' un'ora di ciclo garantito: se la finestra viene
     ridotta a icona, coperta o il PC va in sospensione, il browser SOSPENDE
     l'animazione e con essa questo controllo. Al risveglio ci si ritroverebbe
     con un'assenza apparente di ore e il PC verrebbe bloccato nell'istante
     esatto in cui ti risiedi. Quando fra due controlli passa molto piu' del
     dovuto, il conteggio riparte da zero: di quel buco non sappiamo nulla e
     l'unica ipotesi onesta e' non agire. */
  const SALTO_MS = 5000;
  const ERRORI_VOLTO_MAX = 5;   // errori di fila oltre i quali la presenza si arrende

  /* --- Soglie --- */
  const ARM_HOLD_MS = 600;      // quanto tenere il palmo aperto per armare
  const DISARM_MS = 4000;       // inattività dopo cui si disarma da solo

  /* Estensione delle dita, misurata come distanza punta-polso in unità di
     palmo: ~1.0 = ripiegato dentro il palmo, ~2.0 = disteso. È molto più
     robusta del confronto punta/nocca, che confondeva pugno e pinch. */
  const FOLDED = 1.30;          // sotto questo valore il dito è ritirato nel palmo
  /* Per capire se un dito è DISTESO non si può usare una soglia assoluta uguale
     per tutti: il mignolo è molto più corto del medio e non la raggiungerebbe
     mai, quindi la mano aperta non veniva più riconosciuta. Si confronta invece
     la punta con la nocca intermedia dello stesso dito: è un rapporto, quindi
     vale identico per il mignolo e per il medio. */
  const EXT_RATIO = 1.25;

  /* Pugno contro pinch — il confine più delicato di tutti.
     Una soglia assoluta sull'indice era un precipizio: 1.29 = pugno, 1.31 =
     pinch, e con la mano in movimento la misura oscilla proprio lì. Risultato:
     durante uno swipe a pugno chiuso partiva il controllo del volume.

     Qui il confronto è RELATIVO fra dita della stessa mano: nel pugno indice e
     medio sono ripiegati insieme (differenza quasi nulla), nel pinch l'indice e
     il pollice sono protesi in avanti mentre il medio resta giù. Non dipende da
     quanto è grande la mano né da quanto è lontana la webcam. Fra le due soglie
     c'è una zona morta in cui non si decide nulla. */
  const PINCH_SPORGENZA = 0.25;   // quanto indice+pollice devono superare il medio
  const FIST_SPORGENZA = 0.12;    // sotto questo valore le dita sono tutte insieme

  /* Protezioni temporali: una posa vale solo se REGGE per un po', e il pinch
     non può attivarsi mentre la mano corre (quello è uno swipe, non una
     manopola). Entrambe servono a non farsi ingannare dai fotogrammi sfocati. */
  const POSE_STABLE_MS = 130;     // quanto una posa deve durare per contare
  const PINCH_MAX_SPEED = 1.2;    // palmi al secondo oltre cui il pinch è escluso

  /* Il pinch resta valido fino a PINCH_GATE: deve stare BEN SOPRA il valore
     che corrisponde al volume 100, altrimenti aprendo le dita si esce dalla
     posa prima di arrivare in fondo alla scala (era il bug del "non supera il 38%"). */
  const PINCH_GATE = 0.90;      // oltre questa apertura non è più un pinch
  /* La scala del volume si TARA DA SOLA sulla mano di chi la usa: parte
     stretta e si allarga ai valori davvero osservati. Partire larghi era il
     difetto: se la tua mano apriva meno del previsto, il 100% non arrivava mai. */
  const PINCH_MIN = 0.10;       // punto di partenza per "dita unite"
  const PINCH_MAX = 0.28;       // punto di partenza per "dita aperte", volutamente basso
  const PINCH_SPAN_MIN = 0.12;  // corsa minima, per non renderlo ipersensibile
  /* Filtro del pinch — valori scelti misurando, non a occhio: con rumore
     realistico dei landmark danno 1 punto di oscillazione a mano ferma e
     nessun ritardo percepibile sui movimenti rapidi. La media mobile che
     c'era prima ballava il doppio E arrivava con 130 ms di ritardo.
     minCutoff = quanto filtra da fermo; beta = quanto molla la presa in corsa. */
  const PINCH_CUTOFF = 1.0;
  const PINCH_BETA = 5.0;
  const VOL_MIN_DELTA = 1;      // passi da un punto: il filtro regge la finezza
  const VOL_MIN_MS = 60;        // fino a ~16 aggiornamenti al secondo

  /* Swipe misurato in LUNGHEZZE DI PALMO, non in frazioni di inquadratura:
     così la stessa gesticolata funziona uguale da vicino e da lontano. */
  const SWIPE_PALMI = 0.85;     // quanto deve viaggiare la mano
  const SWIPE_MS = 500;         // finestra temporale dello swipe
  const SWIPE_COOLDOWN = 900;   // pausa fra due gesti discreti
  const SWIPE_POSA_MIN = 0.6;   // frazione di fotogrammi che deve avere la posa giusta
  const SWIPE_COERENZA = 0.7;   // frazione di passi che deve andare nella stessa direzione

  /* "Presa": mano aperta che si CHIUDE -> la finestra passa all'altro schermo.
     Sostituisce lo swipe a pugno, che era il gesto meno affidabile: uno swipe
     richiede movimento, il movimento sfoca l'immagine e proprio lì il
     riconoscimento sbagliava. Chiudere la mano è invece una transizione di
     stato, ferma: molto più netta da leggere. Con due schermi non serve nemmeno
     indicare un verso, "l'altro" è univoco. */
  const GRAB_APERTA_MS = 1200;  // quanto resta valida l'apertura che precede
  const GRAB_TENUTA_MS = 320;   // quanto tenere il pugno perché conti
  const GRAB_MAX_SPEED = 1.5;   // se la mano corre non è una presa
  const GRAB_COOLDOWN = 1400;   // pausa fra due spostamenti
  const GRAB_TOLLERANZA_MS = 450; // fotogrammi incerti tollerati senza azzerare

  let landmarker = null;
  let faceDetector = null;
  let onPresence = () => {};
  let stream = null;
  let lettore = null;     // lettore dei fotogrammi della telecamera
  let passiTotali = 0;    // fotogrammi elaborati: serve a misurare il ritmo vero
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
  const filtroPinch = unEuro(PINCH_CUTOFF, PINCH_BETA, 1.0);
  let pinchMaxSeen = PINCH_MAX;   // estremi osservati: la scala si tara sulla mano
  let pinchMinSeen = PINCH_MIN;
  let lastVolSent = -1;
  let lastVolAt = 0;
  let track = [];               // storico posizione del palmo: {x, s, t, kind}
  let pinching = false;
  let spostamentoCorrente = 0;  // swipe in corso, in palmi (per il riscontro)
  let apertaFinoA = 0;          // fino a quando vale "la mano era aperta"
  let pugnoDa = 0;              // da quando il pugno è chiuso
  let ultimoPugno = 0;          // ultimo fotogramma in cui il pugno era certo
  let armatoDa = 0;             // istante di armamento, per non far scattare subito
  let showNumbers = true;       // riscontro numerico per tarare le soglie

  /* ---------- filtro "One Euro" ----------
     Lo standard per l'input continuo da tracciamento. Una media mobile a
     coefficiente fisso deve scegliere: o segue in fretta (e trema), o è
     stabile (e arriva in ritardo). Questo filtro adatta la costante di tempo
     alla velocità: quasi immobile -> filtra molto, quindi puoi regolare il
     volume di un punto per volta; movimento rapido -> filtra poco, quindi non
     senti ritardo. */
  function unEuro(minCutoff, beta, dCutoff) {
    let xPrec = null, dxPrec = 0, tPrec = null;
    const alfa = (taglio, dt) => {
      const tau = 1 / (2 * Math.PI * taglio);
      return 1 / (1 + tau / dt);
    };
    return {
      reset() { xPrec = null; dxPrec = 0; tPrec = null; },
      filtra(x, t) {
        if (xPrec === null) { xPrec = x; tPrec = t; return x; }
        const dt = Math.max(0.001, (t - tPrec) / 1000);
        tPrec = t;
        const dx = (x - xPrec) / dt;
        dxPrec += alfa(dCutoff, dt) * (dx - dxPrec);
        const taglio = minCutoff + beta * Math.abs(dxPrec);
        xPrec += alfa(taglio, dt) * (x - xPrec);
        return xPrec;
      },
    };
  }

  /* ---------- geometria della mano ---------- */
  const dist = (a, b) => Math.hypot(a.x - b.x, a.y - b.y);

  /** Scala della mano: serve a rendere le distanze indipendenti da quanto
      sei lontano dalla webcam. */
  function handScale(lm) {
    return Math.max(dist(lm[0], lm[9]), 1e-6);
  }

  /** Quanto un dito è proteso: distanza punta-polso in unità di palmo. */
  function reach(lm, tip) {
    return dist(lm[0], lm[tip]) / handScale(lm);
  }

  /** Dito disteso: la punta è nettamente più lontana della nocca intermedia
      dello stesso dito. Rapporto, non soglia assoluta: vale per ogni dito. */
  function extended(lm, tip, pip) {
    return reach(lm, tip) > reach(lm, pip) * EXT_RATIO;
  }

  function pose(lm) {
    const idx = reach(lm, 8), mid = reach(lm, 12),
          ring = reach(lm, 16), pinky = reach(lm, 20);
    const thumb = reach(lm, 4);
    const media = (idx + mid + ring + pinky) / 4;
    const pinchDist = dist(lm[4], lm[8]) / handScale(lm);
    const dist_ = [extended(lm, 8, 6), extended(lm, 12, 10),
                   extended(lm, 16, 14), extended(lm, 20, 18)];
    const nDistese = dist_.filter(Boolean).length;

    /* Quanto indice e pollice sporgono OLTRE il medio. È la grandezza che
       separa davvero il pugno dal pinch, ed è un confronto interno alla mano:
       non cambia con la taglia della mano né con la distanza dalla webcam. */
    const sporgenza = Math.min(idx, thumb) - mid;
    const m = { pinchDist, idx, mid, ring, pinky, thumb, media, nDistese, sporgenza };

    /* 1) MANO APERTA per prima: è inequivocabile e non va contesa a nessuno. */
    if (nDistese >= 3) return { kind: "open", ...m };

    /* 2) PINCH prima del pugno: ora è la SPORGENZA a separarli, e un pugno non
          ne ha, quindi questo ordine è sicuro e libera il pugno dal doversi
          difendere. */
    if (sporgenza > PINCH_SPORGENZA && !dist_[1] && !dist_[2]
        && pinchDist < PINCH_GATE) {
      return { kind: "pinch", ...m };
    }

    /* 3) PUGNO: nessun dito disteso e niente che sporga.
          Prima pretendevo anche `media < 1.30`, una soglia ASSOLUTA: un pugno
          tenuto un po' lasco, o di taglio rispetto alla webcam, la superava e
          finiva in "other" — e ogni fotogramma "other" azzerava il conteggio
          della presa, che così non scattava mai. Il conteggio delle dita distese
          è un rapporto interno alla mano e non ha quel problema. */
    if (nDistese <= 1 && sporgenza < FIST_SPORGENZA) return { kind: "fist", ...m };

    /* Fra FIST_SPORGENZA e PINCH_SPORGENZA c'è una zona morta voluta: nel
       dubbio non si decide, invece di oscillare fra pugno e pinch. */
    return { kind: "other", ...m };
  }

  /* ---------- disegno del riscontro visivo ---------- */
  const BONES = [[0,1],[1,2],[2,3],[3,4],[0,5],[5,6],[6,7],[7,8],[5,9],[9,10],
                 [10,11],[11,12],[9,13],[13,14],[14,15],[15,16],[13,17],[17,18],
                 [18,19],[19,20],[0,17]];

  /* Riscontro della presenza: senza, non c'e' modo di sapere se la webcam ti
     vede in faccia, e l'unico modo di collaudare la funzione sarebbe aspettare
     un'ora sperando che succeda qualcosa. */
  function mmss(ms) {
    const s = Math.max(0, Math.floor(ms / 1000));
    return Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0");
  }

  function disegnaPresenza(w) {
    let testo;
    if (!faceDetector) testo = "presenza non disponibile";
    else if (document.hidden) testo = "presenza in pausa (finestra nascosta)";
    else if (!voltoUltimo) testo = "presenza: cerco il volto…";
    else if (voltoVisibile) testo = "volto ✓  ti vedo";
    else {
      const via = performance.now() - voltoUltimo;
      testo = "assente da " + mmss(via) + "  → saluto fra " +
              mmss(ASSENZA_SALUTO_MS - via);
      if (via >= ASSENZA_SALUTO_MS) {
        testo = "assente da " + mmss(via) + "  → blocco fra " +
                mmss(USCITA_MS - via);
      }
    }
    octx.font = "9px Consolas, monospace";
    octx.fillStyle = voltoVisibile ? "rgba(120,255,180,.9)" : "rgba(255,220,120,.9)";
    octx.textAlign = "right";
    octx.fillText(testo, w - 8, 16);
    octx.textAlign = "left";
  }

  function draw(lm, p) {
    if (!octx) return;
    const w = overlay.width, h = overlay.height;
    octx.clearRect(0, 0, w, h);
    /* PRIMA dell'uscita anticipata qui sotto: la presenza non c'entra con le
       mani, e disegnarla dopo la farebbe comparire solo con una mano inquadrata
       — cioe' mai, proprio quando serve controllare se ti vede in faccia. */
    if (showNumbers) disegnaPresenza(w);
    if (!lm) return;
    const kind = p ? p.kind : null;
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
    if (kind) {
      octx.font = "11px Consolas, monospace";
      octx.fillText(kind.toUpperCase(), 8, 16);
    }
    /* Riscontro numerico: serve a tarare le soglie sulla mano vera invece che
       a stima. Sono le stesse grandezze usate da pose(): apertura del pinch e
       quanto è proteso ogni dito (1.0 = ripiegato, 2.0 = disteso). */
    if (p && showNumbers) {
      octx.font = "9px Consolas, monospace";
      octx.fillStyle = "rgba(255,255,255,.85)";
      octx.fillText(`pinch ${p.pinchDist.toFixed(2)}  scala ${pinchMinSeen.toFixed(2)}-${pinchMaxSeen.toFixed(2)}`, 8, h - 18);
      // "sporg" e' il numero che separa pugno e pinch: sotto 0.12 pugno,
      // sopra 0.25 pinch, in mezzo zona morta.
      octx.fillText(
        `sporg ${p.sporgenza.toFixed(2)}  dita ${p.nDistese}/4`
        + `  swipe ${spostamentoCorrente >= 0 ? "+" : ""}`
        + `${spostamentoCorrente.toFixed(1)}/${SWIPE_PALMI}`, 8, h - 7);
    }
  }

  /* ---------- aggancio del pinch ----------
     Solo il pinch ha bisogno di essere "meritato": è l'unico gesto continuo,
     quindi un lampo sbagliato muove subito il volume. Deve quindi durare un
     minimo E avvenire a mano ferma.

     Gli altri gesti passano invece SENZA ritardo: gli swipe si difendono già
     da soli col voto di maggioranza sulla finestra e con la coerenza di
     direzione. Applicare anche a loro l'attesa li rendeva inservibili — una
     gesticolata dura sì e no mezzo secondo, e se ne andava tutta ad aspettare. */
  let pinchDa = 0;

  function agganciaPinch(kind, now) {
    if (kind !== "pinch") {
      pinchDa = 0;                       // uscita immediata, nessun ritardo
      return kind;
    }
    if (velocita(now) > PINCH_MAX_SPEED) {
      pinchDa = 0;                       // la mano corre: è uno swipe
      return "other";
    }
    if (!pinchDa) pinchDa = now;
    return now - pinchDa >= POSE_STABLE_MS ? "pinch" : "other";
  }

  /** Velocità orizzontale della mano, in palmi al secondo. */
  function velocita(now) {
    if (track.length < 2) return 0;
    const a = track[0], b = track[track.length - 1];
    const dt = (b.t - a.t) / 1000;
    if (dt <= 0.001) return 0;
    return Math.abs((b.x - a.x) / Math.max(b.s, 1e-6)) / dt;
  }

  /* ---------- riconoscimento ---------- */
  function classify(lm, now) {
    const grezza = pose(lm);
    lastSeen = now;

    const p = { ...grezza, kind: agganciaPinch(grezza.kind, now) };

    /* Il tracciato della posizione si aggiorna SEMPRE, prima di ogni ramo:
       serve anche al gate di velocità qui sopra. Se lo aggiornassimo solo nel
       ramo swipe, durante un pinch resterebbe vuoto, la velocità risulterebbe
       zero e il gate non potrebbe mai intervenire. */
    track.push({ x: lm[9].x, s: handScale(lm), t: now, kind: p.kind });
    while (track.length && now - track[0].t > SWIPE_MS) track.shift();

    // --- armamento con il palmo aperto tenuto fermo ---
    if (!armed) {
      if (p.kind === "open") {
        if (!openSince) openSince = now;
        if (now - openSince >= ARM_HOLD_MS) {
          armed = true;
          armatoDa = now;
          openSince = 0;
          track = [];
          apertaFinoA = 0;    // serve una NUOVA apertura dopo l'armamento
          pugnoDa = 0;
          ultimoPugno = 0;
          onState({ armed: true });
        }
      } else {
        openSince = 0;
      }
      return p;
    }

    // --- pinch: volume continuo ---
    if (p.kind === "pinch") {
      pinching = true;
      /* Taratura automatica in ENTRAMBE le direzioni: quanto apre la mano
         cambia da persona a persona e con la distanza dalla webcam. La scala
         si allarga ai valori realmente visti, così il fondo scala coincide
         sempre con la TUA massima apertura e il 100% è sempre raggiungibile. */
      if (p.pinchDist > pinchMaxSeen) pinchMaxSeen = Math.min(p.pinchDist, PINCH_GATE);
      if (p.pinchDist < pinchMinSeen) pinchMinSeen = p.pinchDist;
      /* Filtriamo la DISTANZA grezza, non il volume già mappato: il filtro
         lavora così su una grandezza fisica continua e la taratura resta
         coerente anche mentre la scala si allarga. */
      const distFiltrata = filtroPinch.filtra(p.pinchDist, now);
      const span = Math.max(PINCH_SPAN_MIN, pinchMaxSeen - pinchMinSeen);
      let grezzo = Math.min(1, Math.max(0, (distFiltrata - pinchMinSeen) / span));
      // Scatto agli estremi: vicino ai bordi vogliamo 0 e 100 netti, non 96.
      if (grezzo < 0.04) grezzo = 0;
      else if (grezzo > 0.96) grezzo = 1;
      const vol = Math.round(grezzo * 100);
      if (Math.abs(vol - lastVolSent) >= VOL_MIN_DELTA && now - lastVolAt >= VOL_MIN_MS) {
        lastVolSent = vol;
        lastVolAt = now;
        onEvent({ action: "volume", value: vol });
        onState({ armed: true, hint: `volume ${vol}%` });
      }
      return p;
    }
    // Uscita dal pinch: il filtro riparte pulito al prossimo aggancio.
    if (pinching) { pinching = false; filtroPinch.reset(); }

    /* --- presa: mano aperta che si chiude -> finestra sull'altro schermo ---
       Il conteggio del pugno NON viene azzerato da un fotogramma incerto: solo
       una mano chiaramente aperta lo annulla, oppure l'assenza prolungata del
       pugno. Prima bastava un singolo "other" — che con la mano ferma capita
       spesso — per rimettere a zero, e la presa non maturava mai. */
    if (p.kind === "open") {
      apertaFinoA = now + GRAB_APERTA_MS;   // "di recente la mano era aperta"
      pugnoDa = 0;
    } else if (p.kind === "fist") {
      ultimoPugno = now;
      if (!pugnoDa) pugnoDa = now;
      const dopoApertura = now < apertaFinoA;      // è una chiusura deliberata
      const tenuto = now - pugnoDa >= GRAB_TENUTA_MS;
      const fermo = velocita(now) <= GRAB_MAX_SPEED;
      /* Appena armato la mano è ancora aperta: se la si abbassa per riposare,
         la chiusura non deve valere come comando. */
      const armatoDaAbbastanza = now - armatoDa > 700;
      if (dopoApertura && tenuto && fermo && armatoDaAbbastanza
          && now - lastAction > GRAB_COOLDOWN) {
        lastAction = now;
        apertaFinoA = 0;                          // serve riaprire per rifarlo
        pugnoDa = 0;
        track = [];
        onEvent({ action: "window", value: "other" });
        onState({ armed: true, hint: "finestra sull'altro schermo" });
        return p;
      }
    } else if (now - ultimoPugno > GRAB_TOLLERANZA_MS) {
      pugnoDa = 0;                          // pugno davvero abbandonato
    }

    /* --- swipe (solo mano aperta: tracce musicali) ---
       Il tracciato segue il centro del palmo (nocca media), non il polso:
       durante uno swipe di avambraccio il polso si sposta poco ed era facile
       restare sotto soglia. Lo spostamento è in "palmi", quindi vale identico
       da vicino e da lontano. */
    const centro = lm[9].x;
    const scala = handScale(lm);

    if (p.kind === "open" && track.length >= 5
        && now - lastAction > SWIPE_COOLDOWN) {
      const inizio = track[0];
      const spostamento = (centro - inizio.x) / Math.max(scala, 1e-6);
      spostamentoCorrente = spostamento;

      /* Posa: basta la MAGGIORANZA dei fotogrammi, non tutti. Durante uno
         swipe rapido la mano è mossa e il riconoscimento sfarfalla: pretendere
         la posa perfetta su ogni fotogramma faceva scartare quasi ogni gesto. */
      const conformi = track.filter((s) => s.kind === p.kind).length;
      const posaOk = conformi / track.length >= SWIPE_POSA_MIN;

      /* Direzione: i passi devono andare quasi tutti dalla stessa parte,
         altrimenti un tremolio ampio verrebbe letto come swipe. */
      let concordi = 0, passi = 0;
      for (let i = 1; i < track.length; i++) {
        const d = track[i].x - track[i - 1].x;
        if (Math.abs(d) < 1e-4) continue;
        passi++;
        if (Math.sign(d) === Math.sign(spostamento)) concordi++;
      }
      const direzioneOk = passi === 0 || concordi / passi >= SWIPE_COERENZA;

      if (posaOk && direzioneOk && Math.abs(spostamento) >= SWIPE_PALMI) {
        // La webcam specchia: uno spostamento a destra per te è -x qui.
        const verso = spostamento < 0 ? "right" : "left";
        lastAction = now;
        track = [];
        const v = verso === "right" ? "next" : "previous";
        onEvent({ action: "media", value: v });
        onState({ armed: true, hint: v === "next" ? "traccia avanti" : "traccia indietro" });
      }
    }
    return p;
  }

  /* ---------- presenza ----------
     Macchina a stati semplice: sei qui o non ci sei. Il passaggio richiede
     conferma nel tempo in entrambe le direzioni, così un volto girato per un
     istante non ti fa "uscire" e un passante non ti fa "arrivare". */
  let presente = false;   // ai fini del SALUTO (soglia corta)
  let uscito = false;     // ai fini del BLOCCO (soglia lunga): impedisce di ripeterlo
  let voltoDa = 0;        // da quando si vede un volto senza interruzioni
  let voltoUltimo = 0;    // ultimo istante in cui si e' visto un volto
  let preavvisato = false;
  let prossimoControlloVolto = 0;
  let erroriVolto = 0;    // fallimenti consecutivi del rilevatore
  let voltoVisibile = false;  // ultimo esito, mostrato sull'anteprima

  function aggiornaPresenza(now, fonte, manoInQuadro) {
    if (!faceDetector || now < prossimoControlloVolto) return;
    /* Senza un fotogramma valido detectForVideo lancia, e quelle eccezioni
       venivano contate come "guasto del rilevatore": dopo cinque la presenza
       si spegneva da sola per sempre, cioe' moriva proprio mentre si
       accendeva. */
    if (!fonte) return;
    // Quanto tempo e' passato davvero dal controllo precedente: se il ciclo era
    // sospeso il salto e' enorme e la misura dell'assenza non vale piu' nulla.
    const atteso = prossimoControlloVolto;
    prossimoControlloVolto = now + VOLTO_OGNI_MS;
    if (voltoUltimo && now - atteso > SALTO_MS) {
      voltoUltimo = now;      // riparte il conteggio: serve un'ora osservata davvero
      voltoDa = 0;
      if (preavvisato) { preavvisato = false; onPresence({ tipo: "annulla" }); }
      return;
    }

    /* Una mano in quadro e' gia' la prova che sei li': non serve scomodare il
       rilevatore di volti, che costa e ruberebbe tempo proprio al gesto in
       corso. Vale anche quando guardi altrove mentre gesticoli. */
    if (manoInQuadro) {
      voltoUltimo = now;
      if (!voltoDa) voltoDa = now;
      uscito = false;
      voltoVisibile = true;
      if (preavvisato) { preavvisato = false; onPresence({ tipo: "annulla" }); }
      if (!presente && now - voltoDa >= ARRIVO_CONFERMA_MS) {
        presente = true;
        onPresence({ tipo: "arrivo" });
      }
      return;
    }

    let visto = false;
    try {
      const r = faceDetector.detectForVideo(fonte, now);
      visto = !!(r && r.detections && r.detections.length);
      erroriVolto = 0;
      voltoVisibile = visto;   // solo per il riscontro a schermo
    } catch (e) {
      /* Un fotogramma storto ogni tanto e' normale. Un errore PERSISTENTE no:
         misurato in browser, basta un timestamp fuori ordine e il grafo di
         MediaPipe resta rotto per sempre, senza piu' guarire. Continuare a
         chiamarlo in silenzio darebbe una presenza che sembra attiva e non lo
         e' — il peggiore dei modi di fallire. Meglio spegnerla e dirlo. */
      if (++erroriVolto >= ERRORI_VOLTO_MAX) {
        faceDetector = null;
        onPresence({ tipo: "guasto" });
      }
      return;
    }

    if (visto) {
      voltoUltimo = now;
      if (!voltoDa) voltoDa = now;
      uscito = false;
      if (preavvisato) {        // eri tornato prima che bloccasse
        preavvisato = false;
        onPresence({ tipo: "annulla" });
      }
      if (!presente && now - voltoDa >= ARRIVO_CONFERMA_MS) {
        presente = true;
        onPresence({ tipo: "arrivo" });
      }
      return;
    }

    voltoDa = 0;
    if (!voltoUltimo) return;   // non ti ha mai visto: non c'e' assenza da misurare
    const assenteDa = now - voltoUltimo;

    /* Soglia corta: smetti di risultare "presente", cosi' al ritorno ti
       risaluta. Non produce nessun evento di per se': andarsene non si
       annuncia, si annuncia il tornare. */
    if (presente && assenteDa >= ASSENZA_SALUTO_MS) presente = false;

    // Soglia lunga: il blocco, una volta sola per ogni assenza.
    if (uscito) return;
    if (!preavvisato && assenteDa >= USCITA_PREAVVISO_MS) {
      preavvisato = true;
      onPresence({ tipo: "preavviso" });
    }
    if (assenteDa >= USCITA_MS) {
      uscito = true;
      preavvisato = false;
      onPresence({ tipo: "uscita" });
    }
  }

  /* ---------- ciclo principale ----------
     Un fotogramma, un passo. La sorgente puo' essere un VideoFrame che arriva
     dalla telecamera oppure l'elemento <video>: a MediaPipe vanno bene
     entrambi, e al resto del codice non interessa la differenza. */
  function passo(fonte) {
    passiTotali++;
    const now = performance.now();
    let misure = null;
    let lm = null;
    if (fonte) {
      try {
        const res = landmarker.detectForVideo(fonte, now);
        if (res && res.landmarks && res.landmarks.length) {
          lm = res.landmarks[0];
          misure = classify(lm, now);
        }
      } catch (e) { /* fotogramma saltato: non è un problema */ }
      /* Se una mano e' in quadro sei ovviamente alla scrivania: vale come
         presenza, e in piu' evita di far girare il rilevatore di volti proprio
         mentre stai gesticolando, cioe' quando ogni millisecondo serve alla
         mano. */
      try { aggiornaPresenza(now, fonte, !!lm); } catch (e) { /* mai fermare il ciclo */ }
    }

    // Disarmo automatico: se la mano sparisce, i comandi si spengono.
    if (armed && now - lastSeen > DISARM_MS) {
      armed = false;
      filtroPinch.reset();
      apertaFinoA = 0;
      pugnoDa = 0;
      ultimoPugno = 0;
      lastVolSent = -1;
      track = [];
      onState({ armed: false });
    }
    // A finestra coperta non c'e' niente da disegnare: e' lavoro buttato.
    if (!document.hidden) draw(lm, misure);
  }

  /* Ciclo guidato dalla TELECAMERA, non dal disegno dello schermo.
     requestAnimationFrame sembrava la scelta ovvia, ma misurato: con la
     finestra coperta da un'altra applicazione Chrome lo ferma del tutto —
     zero chiamate al secondo — e i gesti morivano appena mettevi Spotify
     davanti alla HUD. La telecamera invece continua a consegnare fotogrammi
     anche da coperta: prendendoli da lei il riconoscimento gira uguale sopra
     e sotto (misurati 20/s in entrambi i casi, zero errori). */
  async function cicloDaTelecamera(track_) {
    lettore = new MediaStreamTrackProcessor({ track: track_ }).readable.getReader();
    while (running) {
      let frame = null;
      try {
        const r = await lettore.read();
        if (r.done) break;
        frame = r.value;
      } catch (e) { break; }
      try {
        passo(frame);
      } finally {
        // Obbligatorio: un fotogramma non chiuso blocca la telecamera.
        frame.close();
      }
    }
  }

  // Ripiego per browser senza MediaStreamTrackProcessor: com'era prima.
  function cicloDaSchermo() {
    if (!running) return;
    passo(video.readyState >= 2 ? video : null);
    requestAnimationFrame(cicloDaSchermo);
  }

  /* ---------- API pubblica ---------- */
  async function start(opts) {
    onEvent = opts.onEvent || onEvent;
    onState = opts.onState || onState;
    onPresence = opts.onPresence || onPresence;
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
      /* Rilevatore di volti: serve solo a sapere se sei alla scrivania, quindi
         gira a mezzo hertz e non pesa. Se non si carica, i gesti funzionano
         lo stesso: la presenza e' un di piu', non deve poter rompere nulla. */
      for (const delegate of ["GPU", "CPU"]) {
        try {
          faceDetector = await vision.FaceDetector.createFromOptions(fileset, {
            baseOptions: { modelAssetPath: MODEL_VOLTO, delegate },
            runningMode: "VIDEO",
          });
          break;
        } catch (e) { faceDetector = null; }
      }
      // Se non parte nemmeno su CPU va detto: una presenza che tace perche' e'
      // rotta e' indistinguibile da una presenza che tace perche' sei li'.
      if (!faceDetector) onPresence({ tipo: "guasto" });
    }

    stream = await navigator.mediaDevices.getUserMedia({
      video: { width: 640, height: 480, facingMode: "user" },
    });
    video.srcObject = stream;
    await video.play();
    running = true;
    armed = false;
    lastSeen = performance.now();

    const traccia = stream.getVideoTracks()[0];
    let daTelecamera = false;
    if (traccia && typeof window.MediaStreamTrackProcessor === "function") {
      try { cicloDaTelecamera(traccia); daTelecamera = true; } catch (e) { daTelecamera = false; }
    }
    if (!daTelecamera) requestAnimationFrame(cicloDaSchermo);
    return { sottoCoperta: daTelecamera };
  }

  function stop() {
    running = false;
    armed = false;
    if (lettore) {
      // Sveglia il ciclo fermo su read(), altrimenti resterebbe appeso.
      try { lettore.cancel(); } catch (e) { /* gia' chiuso */ }
      lettore = null;
    }
    presente = false; uscito = false; voltoDa = 0; voltoUltimo = 0;
    preavvisato = false; erroriVolto = 0; prossimoControlloVolto = 0;
    voltoVisibile = false;
    filtroPinch.reset();
    apertaFinoA = 0;
    pugnoDa = 0;
    ultimoPugno = 0;
    pinchMaxSeen = PINCH_MAX;   // la taratura riparte alla prossima accensione
    pinchMinSeen = PINCH_MIN;
    lastVolSent = -1;
    track = [];
    if (stream) {
      stream.getTracks().forEach((t) => t.stop());   // spegne la spia della webcam
      stream = null;
    }
    if (video) video.srcObject = null;
    if (octx) octx.clearRect(0, 0, overlay.width, overlay.height);
  }

  /** Mostra o nasconde i numeri di taratura sull'anteprima. */
  function toggleNumbers() {
    showNumbers = !showNumbers;
    return showNumbers;
  }

  /* Ritmo reale del riconoscimento. Serve a rispondere con un numero, invece
     che a intuito, alla domanda "i gesti stanno ancora girando?" — soprattutto
     con la finestra coperta, dove non si vede nulla per definizione. */
  function stato() {
    return {
      running,
      passi: passiTotali,
      daTelecamera: !!lettore,
      presenza: faceDetector ? (voltoVisibile ? "volto" : "assente") : "non disponibile",
    };
  }

  window.JarvisGestures = { start, stop, toggleNumbers, stato, isRunning: () => running };
})();
