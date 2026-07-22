/* J.A.R.V.I.S. — logica client: voce, WebSocket, HUD */
(() => {
  "use strict";

  const body = document.body;
  const caption = document.getElementById("caption");
  const stateLabel = document.getElementById("state-label");
  const log = document.getElementById("log");
  const micBtn = document.getElementById("mic");
  const resetBtn = document.getElementById("reset");
  const continuousBox = document.getElementById("continuous");
  const wakeBox = document.getElementById("wakeword");
  const textInput = document.getElementById("text-input");
  const wakeHint = document.getElementById("wake-hint");

  const STATE_TEXT = {
    boot: "AVVIO", idle: "IN ATTESA", listening: "IN ASCOLTO",
    thinking: "ELABORO", speaking: "RISPONDO", error: "ERRORE",
  };

  /* Varianti con cui il riconoscimento vocale italiano trascrive "Jarvis". */
  const WAKE_RE = /\b(jarvis|jarviss|jervis|giarvis|charvis|djarvis|jarbis)\b[\s,.:;!?]*/i;
  const ARM_TIMEOUT_MS = 9000;

  /* Interruzione a voce: solo queste parole, e solo se sono TUTTO ciò che è
     stato detto. Il microfono resta acceso mentre Jarvis parla, quindi la
     regola dev'essere strettissima: qualunque altra frase viene ignorata,
     altrimenti si torna al loop in cui Jarvis si risponde da solo. */
  const STOP_RE = /^(basta|stop|fermati|fermo|zitto|taci|silenzio|ok basta|va bene basta)$/i;
  const BARGE_GRACE_MS = 800;   // ignora l'inizio: è lì che l'eco è più probabile

  let processing = false;   // Jarvis sta elaborando
  let listening = false;    // riconoscimento attivo
  let speaking = false;     // TTS in corso
  let speakStartedAt = 0;   // per la grazia iniziale dell'interruzione
  let speechEndedAt = 0;    // per scartare la coda dell'eco
  let booted = false;
  let armed = false;        // wake word gia sentita: prossima frase = comando
  let armTimer = null;

  function setState(s) {
    body.dataset.state = s;
    stateLabel.textContent = STATE_TEXT[s] || s.toUpperCase();
  }

  function addEntry(text, cls) {
    const e = document.createElement("div");
    e.className = "entry " + cls;
    const who = { user: "SIGNORE", jarvis: "JARVIS", act: "AZIONE", sys: "SISTEMA" }[cls] || "";
    const label = document.createElement("span");
    label.className = "who";
    label.textContent = who;
    e.appendChild(label);
    e.appendChild(document.createTextNode(text));
    log.appendChild(e);
    log.scrollTop = log.scrollHeight;
  }

  /* ---------- Sequenza di avvio ---------- */
  const BOOT_LINES = [
    "Inizializzazione nucleo…",
    "Interfaccia vocale: online",
    "Accesso al sistema: concesso",
    "Diagnostica hardware: nominale",
    "Buongiorno, signore.",
  ];
  function runBoot() {
    const el = document.getElementById("boot-log");
    const overlay = document.getElementById("boot");
    let i = 0;
    const step = () => {
      if (i < BOOT_LINES.length) {
        const line = document.createElement("div");
        line.textContent = "> " + BOOT_LINES[i++];
        el.appendChild(line);
        setTimeout(step, 340);
        return;
      }
      setTimeout(() => {
        overlay.classList.add("gone");
        booted = true;
        setState("idle");
        caption.textContent = "Ai suoi ordini, signore.";
      }, 500);
    };
    setTimeout(step, 350);
  }

  /* ---------- WebSocket ---------- */
  let ws = null;
  let retry = 0;

  function connect() {
    ws = new WebSocket("ws://" + location.host + "/ws");
    ws.onopen = () => {
      retry = 0;
      if (booted) {
        setState("idle");
        caption.textContent = "Ai suoi ordini, signore.";
      }
    };
    ws.onclose = () => {
      setState("error");
      caption.textContent = "Connessione persa. Riconnessione…";
      retry = Math.min(retry + 1, 6);
      setTimeout(connect, 800 * retry);
    };
    ws.onerror = () => { try { ws.close(); } catch (e) { /* gestito da onclose */ } };
    ws.onmessage = (ev) => {
      let m;
      try { m = JSON.parse(ev.data); } catch (e) { return; }
      if (m.type === "status") {
        if (m.state !== "speaking") setState(m.state);
      } else if (m.type === "action") {
        addEntry(m.text, "act");
        caption.textContent = m.text + "…";
      } else if (m.type === "say") {
        addEntry(m.text, "jarvis");
        speak(m.text);
      } else if (m.type === "wake") {
        // Scorciatoia globale premuta: apri il microfono già armato.
        shutUp();
        initViz();
        arm();
        startListen();
        caption.textContent = "Sì, signore?";
      } else if (m.type === "announce") {
        accodaAnnuncio(m.text);
      } else if (m.type === "gesture_ack") {
        addEntry(m.text, "act");           // solo i gesti discreti, non il volume
      } else if (m.type === "done") {
        processing = false;
        if (!speaking) goIdle();
      }
    };
  }

  function goIdle() {
    setState("idle");
    svuotaAnnunci();          // ora che è libero, può dire ciò che aveva in serbo
    if (continuousBox.checked) startListen();
  }

  /* ---------- Annunci di iniziativa ----------
     Jarvis può parlare per primo (download completato, batteria scarica,
     briefing). Non deve però interrompere una risposta in corso né accavallarsi
     a una domanda: se è occupato l'annuncio aspetta il momento buono. */
  const annunci = [];

  function accodaAnnuncio(testo) {
    if (!testo) return;
    annunci.push(testo);
    svuotaAnnunci();
  }

  function svuotaAnnunci() {
    /* Si guarda solo se sta elaborando o parlando. NON si guarda "listening":
       in ascolto continuo il microfono è quasi sempre acceso, e gli annunci
       non uscirebbero mai. Che il microfono risenta la propria voce è già
       coperto dal filtro anti-eco. */
    if (!annunci.length || processing || speaking) return;
    const testo = annunci.shift();
    addEntry(testo, "jarvis");
    speak(testo);
  }

  function sendCommand(text) {
    if (!text.trim()) return;
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      addEntry("Connessione al server non disponibile.", "sys");
      return;
    }
    disarm();
    addEntry(text, "user");
    processing = true;
    setState("thinking");
    caption.textContent = "Elaboro…";
    ws.send(JSON.stringify({ type: "command", text }));
  }

  /* ---------- Sintesi vocale (TTS) — voce da maggiordomo ----------
     Preferiamo le voci neurali generate dal server (edge-tts): su Chrome le
     voci di sistema italiane sono spesso femminili e metalliche. Se il server
     non risponde, si ripiega automaticamente sulla voce del browser. */
  const voiceSelect = document.getElementById("voice-select");
  const VOICE_KEY = "jarvis-voce";
  // Cadenza pacata e composta: un maggiordomo non ha fretta.
  const RATE = 0.96, PITCH = 0.82;

  let voice = null;          // voce del browser (ripiego)
  let voices = [];           // voci del browser
  let serverVoices = [];     // voci neurali del server
  let selected = null;       // {kind:"server"|"browser", id}
  let currentAudio = null;   // audio del server in riproduzione

  // Nomi maschili italiani (le voci "buone" da maggiordomo) e femminili da evitare.
  const MALE_IT = /giuseppe|cosimo|diego|calvino|gianni|lisandro|marcello|davide|thomas|guy/i;
  const FEMALE = /elsa|isabella|bianca|elisa|federica|silvia|caterina|female|donna|google italiano/i;
  const GOOD_QUALITY = /natural|neural|online|premium|enhanced/i;

  function scoreVoice(v) {
    const name = (v.name || "").toLowerCase();
    const lang = (v.lang || "").toLowerCase();
    let s = 0;
    if (lang.startsWith("it")) s += 100;
    else if (lang.startsWith("en")) s += 8;      // ripiego decoroso se manca l'italiano
    if (GOOD_QUALITY.test(name)) s += 45;         // voci neurali: molto più naturali
    if (MALE_IT.test(name)) s += 40;              // maschile = maggiordomo
    if (FEMALE.test(name)) s -= 30;
    return s;
  }

  function rebuildVoiceMenu() {
    voiceSelect.replaceChildren();

    // 1) Voci neurali del server: le migliori, in cima.
    if (serverVoices.length) {
      const g = document.createElement("optgroup");
      g.label = "Voci neurali (consigliate)";
      for (const v of serverVoices) {
        const o = document.createElement("option");
        o.value = "server:" + v.id;
        o.textContent = v.label;
        g.appendChild(o);
      }
      voiceSelect.appendChild(g);
    }

    // 2) Voci del browser, italiane per prime.
    if (voices.length) {
      const g = document.createElement("optgroup");
      g.label = "Voci del browser";
      const sorted = [...voices].sort((a, b) => scoreVoice(b) - scoreVoice(a));
      for (const v of sorted) {
        const o = document.createElement("option");
        o.value = "browser:" + v.voiceURI;
        o.textContent = `${v.name} (${v.lang})`;
        g.appendChild(o);
      }
      voiceSelect.appendChild(g);
    }

    // Ripristina la scelta salvata, altrimenti la migliore disponibile.
    const saved = localStorage.getItem(VOICE_KEY);
    const valid = saved && [...voiceSelect.options].some((o) => o.value === saved);
    const fallback = serverVoices.length
      ? "server:" + serverVoices[0].id
      : (voices.length ? "browser:" + [...voices].sort((a, b) => scoreVoice(b) - scoreVoice(a))[0].voiceURI : "");
    applyVoice(valid ? saved : fallback, false);
  }

  function applyVoice(value, save) {
    if (!value) return;
    const [kind, id] = [value.slice(0, value.indexOf(":")), value.slice(value.indexOf(":") + 1)];
    selected = { kind, id };
    if (kind === "browser") voice = voices.find((v) => v.voiceURI === id) || null;
    voiceSelect.value = value;
    if (save) localStorage.setItem(VOICE_KEY, value);
  }

  function loadBrowserVoices() {
    if (!("speechSynthesis" in window)) return;
    voices = speechSynthesis.getVoices();
    rebuildVoiceMenu();
  }

  async function loadServerVoices() {
    try {
      const r = await (await fetch("/api/voices")).json();
      serverVoices = r.voices || [];
    } catch (e) { serverVoices = []; }
    rebuildVoiceMenu();
  }

  if ("speechSynthesis" in window) {
    loadBrowserVoices();
    speechSynthesis.onvoiceschanged = loadBrowserVoices;
  }
  loadServerVoices();

  voiceSelect.addEventListener("change", () => {
    applyVoice(voiceSelect.value, true);
    shutUp();
    speak("Sono a sua disposizione, signore.");   // anteprima immediata
  });

  /* Ogni frase ha un numero progressivo: se ne arriva una nuova, quelle
     ancora in volo vengono scartate invece di sovrapporsi. */
  let speechGen = 0;
  const ECHO_COOLDOWN_MS = 550;   // pausa dopo la voce, prima di riascoltare

  function onSpeakEnd(gen) {
    if (gen !== speechGen) return;      // sorpassata da una frase più recente
    speaking = false;
    speechEndedAt = Date.now();
    // Piccola attesa: la coda dell'audio e il riverbero non devono finire
    // dentro il microfono e farsi trascrivere come un comando.
    setTimeout(() => {
      if (gen === speechGen && !speaking && !processing) goIdle();
    }, ECHO_COOLDOWN_MS);
  }

  /* Voce neurale dal server: ritorna true se è riuscita a partire. */
  async function speakServer(text, voiceId, gen) {
    let url = null;
    try {
      const r = await fetch("/api/tts", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text, voice: voiceId }),
      });
      if (!r.ok) return false;
      const blob = await r.blob();
      if (gen !== speechGen) return true;   // nel frattempo è arrivata altra voce
      url = URL.createObjectURL(blob);
      const audio = new Audio(url);
      currentAudio = audio;
      collegaAnalisiVoce(audio);   // il reattore pulserà sulla voce vera
      audio.onended = audio.onerror = () => {
        URL.revokeObjectURL(url);
        if (currentAudio === audio) currentAudio = null;
        onSpeakEnd(gen);
      };
      await audio.play();       // può fallire se l'autoplay è bloccato
      return true;
    } catch (e) {
      if (url) URL.revokeObjectURL(url);
      currentAudio = null;
      return false;
    }
  }

  function speakBrowser(text, gen) {
    if (!("speechSynthesis" in window)) { onSpeakEnd(gen); return; }
    speechSynthesis.cancel();
    const u = new SpeechSynthesisUtterance(text);
    if (voice) { u.voice = voice; u.lang = voice.lang; }
    else u.lang = "it-IT";
    u.rate = RATE;
    u.pitch = PITCH;
    u.onend = u.onerror = () => onSpeakEnd(gen);
    speechSynthesis.speak(u);
  }

  async function speak(text) {
    const gen = ++speechGen;

    /* Marchiamo "sta parlando" SUBITO, non quando parte l'audio: fra la
       richiesta al server e il primo campione passa quasi un secondo, e in
       quella finestra il messaggio "done" riaccenderebbe il microfono. Jarvis
       si risentirebbe da solo e finirebbe in loop. */
    speaking = true;
    speakStartedAt = Date.now();
    /* Vale per una frase sola: la voce del browser non passa dall'analizzatore
       e il reattore deve tornare all'animazione sintetica invece di restare
       piatto sull'ultimo spettro. */
    voceAgganciata = false;
    disarm();                   // una parola d'attivazione non deve sopravvivere alla risposta
    /* In ascolto continuo il microfono resta acceso, così puoi interromperlo
       dicendo "basta". È sicuro perché mentre parla l'unica azione ammessa è
       zittirlo: nessuna frase può diventare un comando, quindi un falso
       riconoscimento non può innescare il loop. Fuori dall'ascolto continuo il
       microfono si spegne come prima. */
    if (!continuousBox.checked) stopListen();
    rememberSpoken(text);       // per riconoscere la propria eco
    setState("speaking");
    caption.textContent = text;

    if (selected && selected.kind === "server") {
      if (await speakServer(text, selected.id, gen)) return;
      if (gen !== speechGen) return;   // sorpassata mentre attendevamo il server
      // Server muto (rete assente, autoplay bloccato): ripiego sul browser.
    }
    speakBrowser(text, gen);
  }

  function shutUp() {
    speechGen++;                // invalida le frasi ancora in volo
    if (currentAudio) {
      try { currentAudio.pause(); } catch (e) { /* gia fermo */ }
      currentAudio = null;
    }
    if ("speechSynthesis" in window) speechSynthesis.cancel();
    speaking = false;
  }

  /* ---------- Riconoscimento vocale (STT) ---------- */
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  let recog = null;
  let micGranted = false;

  if (SR) {
    recog = new SR();
    recog.lang = "it-IT";
    recog.interimResults = true;
    recog.continuous = true;

    recog.onstart = () => {
      listening = true;
      micGranted = true;
      micBtn.classList.add("active");
      if (!processing && !speaking) setState("listening");
    };
    recog.onerror = (e) => {
      listening = false;
      micBtn.classList.remove("active");
      if (e.error === "not-allowed" || e.error === "service-not-allowed") {
        continuousBox.checked = false;
        caption.textContent = "Microfono negato. Consenti l'accesso e riprova.";
        addEntry("Accesso al microfono negato dal browser.", "sys");
      }
    };
    recog.onend = () => {
      listening = false;
      micBtn.classList.remove("active");
      // In ascolto continuo il motore si ferma da solo: lo riavviamo.
      // Anche mentre Jarvis parla, per poterlo interrompere a voce.
      if (continuousBox.checked && !processing) {
        setTimeout(startListen, 250);
      }
    };
    recog.onresult = (ev) => {
      let interim = "", final = "";
      for (let i = ev.resultIndex; i < ev.results.length; i++) {
        const r = ev.results[i];
        if (r.isFinal) final += r[0].transcript;
        else interim += r[0].transcript;
      }
      if (interim && !processing) caption.textContent = "« " + interim.trim() + " »";
      if (final.trim()) handleFinal(final.trim());
    };
  } else {
    caption.textContent = "Riconoscimento vocale non disponibile. Usa il campo di testo.";
  }

  /* ---------- Filtro anti-eco ----------
     Ultima difesa: se l'audio di Jarvis rientra nel microfono (casse alte,
     cuffie aperte, ritardo di spegnimento), la trascrizione somiglia molto a
     quello che ha appena detto. In quel caso la scartiamo invece di trattarla
     come un comando: senza questo, ogni risposta ne innesca un'altra. */
  const spokenHistory = [];
  const ECHO_MEMORY_MS = 20000;

  function normalizeText(s) {
    return (s || "")
      .toLowerCase()
      .normalize("NFD").replace(/[̀-ͯ]/g, "")   // via gli accenti
      .replace(/[^a-z0-9\s]/g, " ")
      .replace(/\s+/g, " ")
      .trim();
  }

  function rememberSpoken(text) {
    const norm = normalizeText(text);
    if (norm) spokenHistory.push({ norm, t: Date.now() });
    while (spokenHistory.length > 8) spokenHistory.shift();
  }

  function isEcho(text) {
    const norm = normalizeText(text);
    if (!norm) return false;
    const words = norm.split(" ").filter(Boolean);
    if (!words.length) return false;
    const now = Date.now();
    for (const past of spokenHistory) {
      if (now - past.t > ECHO_MEMORY_MS) continue;

      /* Contenimento: la trascrizione è un pezzo di ciò che Jarvis ha detto.
         Solo su frasi non banali: sotto i 15 caratteri un "sono" o un "va bene"
         scatterebbe su comandi legittimi. */
      if (norm.length >= 15 && (past.norm.includes(norm) || norm.includes(past.norm))) {
        return true;
      }

      /* Sovrapposizione lessicale: solo su frasi lunghe. Su un comando corto
         come "che ore sono" bastavano due parole comuni ("che", "sono") per
         superare la soglia e farlo scartare per errore. */
      if (words.length >= 6) {
        const vocab = new Set(past.norm.split(" "));
        const shared = words.filter((w) => vocab.has(w)).length;
        if (shared / words.length >= 0.7) return true;
      }
    }
    return false;
  }

  function needsWake() {
    // La parola d'attivazione serve solo in ascolto continuo e se non siamo gia "armati".
    return continuousBox.checked && wakeBox.checked && !armed;
  }

  function arm() {
    armed = true;
    clearTimeout(armTimer);
    armTimer = setTimeout(disarm, ARM_TIMEOUT_MS);
    body.classList.add("armed");
    caption.textContent = "Sì, signore?";
  }

  function disarm() {
    armed = false;
    clearTimeout(armTimer);
    body.classList.remove("armed");
  }

  function handleFinal(transcript) {
    // Jarvis che risente se stesso: ignora, altrimenti si autoalimenta.
    if (isEcho(transcript)) return;

    /* Interruzione a voce. Vale solo mentre parla, solo per le parole di stop
       pronunciate da sole, e solo dopo una breve grazia iniziale. */
    if (speaking) {
      const pulito = transcript.trim().replace(/[.,!?;:]+$/, "");
      if (STOP_RE.test(pulito) && Date.now() - speakStartedAt > BARGE_GRACE_MS) {
        shutUp();
        caption.textContent = "Come desidera.";
        addEntry("Interrotto.", "sys");
        if (!processing) goIdle();
      }
      return;                             // mentre parla, nient'altro passa
    }

    if (processing) return;               // niente comandi mentre elabora
    // Coda dell'eco: quello che arriva subito dopo la voce è quasi sempre suo.
    if (Date.now() - speechEndedAt < 500) return;
    if (!needsWake()) {
      sendCommand(transcript);
      return;
    }
    const m = transcript.match(WAKE_RE);
    if (!m) return;                       // rumore di fondo: ignora
    const after = transcript.slice(m.index + m[0].length).trim();
    if (after) sendCommand(after);        // "Jarvis, apri Firefox"
    else arm();                           // solo "Jarvis": resta in ascolto del comando
  }

  function startListen() {
    if (!recog || listening || processing) return;
    // Mentre parla si ascolta solo in modalità continua (per il "basta").
    if (speaking && !continuousBox.checked) return;
    try { recog.start(); } catch (e) { /* gia avviato */ }
  }
  function stopListen() {
    if (recog && listening) { try { recog.stop(); } catch (e) { /* gia fermo */ } }
  }

  micBtn.addEventListener("click", () => {
    if (speaking) { shutUp(); }          // interrompi Jarvis e ascolta
    if (listening) { stopListen(); disarm(); return; }
    initViz();
    arm();                                // il click vale come parola d'attivazione
    startListen();
  });

  continuousBox.addEventListener("change", () => {
    if (continuousBox.checked) { initViz(); startListen(); }
    else { stopListen(); disarm(); }
  });

  wakeBox.addEventListener("change", () => {
    wakeHint.textContent = wakeBox.checked ? "di' «Jarvis»" : "sempre attivo";
    disarm();
  });

  textInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && textInput.value.trim()) {
      shutUp();
      sendCommand(textInput.value.trim());
      textInput.value = "";
    }
  });

  resetBtn.addEventListener("click", async () => {
    shutUp();
    try { await fetch("/api/reset", { method: "POST" }); } catch (e) { /* offline */ }
    log.replaceChildren();
    addEntry("Conversazione azzerata.", "sys");
    caption.textContent = "Memoria di sessione azzerata.";
  });

  /* ---------- Visualizzatore audio attorno al reattore ---------- */
  const canvas = document.getElementById("viz");
  const ctx = canvas.getContext("2d");
  let analyser = null, freq = null, vizStarted = false;

  /* Analisi della VOCE di Jarvis: finora, mentre parlava, l'anello attorno al
     reattore disegnava un'onda finta. Agganciando l'audio del TTS il reattore
     pulsa sulle sillabe reali. */
  let audioCtx = null, vozAnalyser = null, vozFreq = null, voceAgganciata = false;

  function collegaAnalisiVoce(audio) {
    try {
      if (!audioCtx) {
        audioCtx = new (window.AudioContext || window.webkitAudioContext)();
        vozAnalyser = audioCtx.createAnalyser();
        vozAnalyser.fftSize = 128;
        vozAnalyser.smoothingTimeConstant = 0.6;
        vozFreq = new Uint8Array(vozAnalyser.frequencyBinCount);
        vozAnalyser.connect(audioCtx.destination);
      }
      /* Attenzione: una volta agganciato, l'audio dell'elemento passa SOLO dal
         grafo. Se il contesto è sospeso (autoplay non ancora sbloccato) la voce
         diventerebbe muta, quindi in quel caso non si aggancia niente: si
         chiede lo sblocco e questa frase esce dall'elemento come sempre. */
      if (audioCtx.state !== "running") { audioCtx.resume(); return; }
      // Una sorgente per elemento: ogni frase ha il suo <audio>, quindi va bene.
      audioCtx.createMediaElementSource(audio).connect(vozAnalyser);
      voceAgganciata = true;
    } catch (e) {
      /* Se il collegamento fallisce l'audio si sente lo stesso: il reattore
         torna semplicemente all'animazione sintetica. */
    }
  }

  function initViz() {
    if (vizStarted || !navigator.mediaDevices) return;
    vizStarted = true;
    navigator.mediaDevices.getUserMedia({ audio: true }).then((stream) => {
      const ac = new (window.AudioContext || window.webkitAudioContext)();
      const src = ac.createMediaStreamSource(stream);
      analyser = ac.createAnalyser();
      analyser.fftSize = 128;
      freq = new Uint8Array(analyser.frequencyBinCount);
      src.connect(analyser);
    }).catch(() => { vizStarted = false; });
  }

  function drawViz() {
    requestAnimationFrame(drawViz);
    const w = canvas.width, h = canvas.height;
    ctx.clearRect(0, 0, w, h);
    const glow = getComputedStyle(body).getPropertyValue("--glow").trim() || "#35e0ff";
    const bars = 72, R = 175;
    /* Tre sorgenti, in ordine di priorità: la voce di Jarvis mentre parla, il
       microfono mentre ascolta, l'animazione sintetica quando non c'è audio. */
    const voce = speaking && voceAgganciata && vozAnalyser;
    const live = !voce && analyser && listening;
    if (voce) vozAnalyser.getByteFrequencyData(vozFreq);
    else if (live) analyser.getByteFrequencyData(freq);

    ctx.save();
    ctx.translate(w / 2, h / 2);
    for (let i = 0; i < bars; i++) {
      let v;
      if (voce) {
        /* La voce vive nelle prime bande (sotto i ~4 kHz): distribuirla su
           tutte e 64 lascerebbe metà anello immobile. Si usano solo le bande
           utili e si specchiano sui due lati, così il reattore pulsa
           simmetrico sulle sillabe invece di scorrere come un equalizzatore. */
        const utili = 16;
        const d = Math.abs(bars / 2 - i) / (bars / 2);   // 0 al centro, 1 ai lati
        v = Math.min(1, (vozFreq[Math.floor(d * (utili - 1))] / 255) * 1.35);
        v = Math.max(v, 0.08);   // nelle pause fra le parole resta acceso, non morto
      } else if (live) {
        v = freq[i % freq.length] / 255;
      } else {
        const t = Date.now() / 600;
        const amp = speaking ? 0.5 : (processing ? 0.32 : 0.14);
        v = amp * (0.5 + 0.5 * Math.sin(t + i * 0.5));
      }
      const len = 10 + v * 46;
      ctx.save();
      ctx.rotate((i / bars) * Math.PI * 2);
      ctx.fillStyle = glow;
      ctx.globalAlpha = 0.35 + v * 0.65;
      ctx.fillRect(-1.6, -R, 3.2, -len);
      ctx.restore();
    }
    ctx.restore();
  }
  drawViz();

  /* ---------- Controllo a gesti (webcam) ---------- */
  const gestBox = document.getElementById("gestures");
  const camWrap = document.getElementById("cam-wrap");
  const camState = document.getElementById("cam-state");
  const GEST_KEY = "jarvis-gesti";
  let gestHintTimer = null;

  function sendGesture(ev) {
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    ws.send(JSON.stringify({ type: "gesture", action: ev.action, value: ev.value }));
  }

  /* Presenza: la webcam vede solo se c'è un volto, e questo è tutto ciò che
     lascia il browser. Nessuna immagine e nessuna identità vengono trasmesse. */
  function sendPresence(ev) {
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    ws.send(JSON.stringify({ type: "presence", event: ev.tipo }));
  }

  function gestureState(s) {
    body.classList.toggle("gest-armed", !!s.armed);
    if (s.hint) {
      camState.textContent = s.hint;
      clearTimeout(gestHintTimer);
      gestHintTimer = setTimeout(() => {
        camState.textContent = s.armed ? "attivo" : "palmo aperto per attivare";
      }, 1400);
      return;
    }
    clearTimeout(gestHintTimer);
    camState.textContent = s.armed ? "attivo" : "palmo aperto per attivare";
  }

  async function startGestures() {
    camWrap.hidden = false;
    camState.textContent = "carico il modello…";
    try {
      await window.JarvisGestures.start({
        video: document.getElementById("cam"),
        overlay: document.getElementById("cam-overlay"),
        onEvent: sendGesture,
        onState: gestureState,
        onPresence: sendPresence,
      });
      camState.textContent = "palmo aperto per attivare";
      localStorage.setItem(GEST_KEY, "1");
      addEntry("Controllo a gesti attivo. Palmo aperto per armare.", "sys");
    } catch (e) {
      // Webcam negata, offline, oppure modello non scaricabile.
      gestBox.checked = false;
      camWrap.hidden = true;
      localStorage.removeItem(GEST_KEY);
      addEntry("Gesti non disponibili: " + (e && e.message ? e.message : e), "sys");
    }
  }

  function stopGestures() {
    window.JarvisGestures.stop();
    camWrap.hidden = true;
    body.classList.remove("gest-armed");
    localStorage.removeItem(GEST_KEY);
  }

  gestBox.addEventListener("change", () => {
    if (gestBox.checked) startGestures();
    else stopGestures();
  });

  // Click sull'anteprima: mostra/nasconde i numeri di taratura.
  camWrap.addEventListener("click", () => {
    const attivi = window.JarvisGestures.toggleNumbers();
    camState.textContent = attivi ? "numeri visibili" : "numeri nascosti";
  });

  // Ripristina la scelta, ma solo dopo un gesto dell'utente: il browser non
  // concede la webcam senza interazione.
  if (localStorage.getItem(GEST_KEY) === "1") {
    const riprendi = () => {
      document.removeEventListener("click", riprendi);
      gestBox.checked = true;
      startGestures();
    };
    document.addEventListener("click", riprendi, { once: true });
  }

  /* ---------- Orologio ---------- */
  const days = ["DOMENICA", "LUNEDÌ", "MARTEDÌ", "MERCOLEDÌ", "GIOVEDÌ", "VENERDÌ", "SABATO"];
  const months = ["GEN", "FEB", "MAR", "APR", "MAG", "GIU", "LUG", "AGO", "SET", "OTT", "NOV", "DIC"];
  function tick() {
    const d = new Date();
    document.getElementById("clock-time").textContent = d.toLocaleTimeString("it-IT");
    document.getElementById("clock-date").textContent =
      `${days[d.getDay()]} ${d.getDate()} ${months[d.getMonth()]} ${d.getFullYear()}`;
  }
  tick();
  setInterval(tick, 1000);

  /* ---------- Statistiche di sistema ---------- */
  function setBar(barId, valId, pct, label) {
    document.getElementById(barId).style.width = Math.min(100, Math.max(0, pct)) + "%";
    document.getElementById(valId).textContent = label;
  }

  async function pollStats() {
    try {
      const s = await (await fetch("/api/stats")).json();
      setBar("bar-cpu", "val-cpu", s.cpu, s.cpu + "%");
      const ramPct = s.ramTotal ? (s.ramUsed / s.ramTotal) * 100 : 0;
      setBar("bar-ram", "val-ram", ramPct, `${s.ramUsed} / ${s.ramTotal} GB`);
      const diskPct = s.diskTotal ? ((s.diskTotal - s.diskFree) / s.diskTotal) * 100 : 0;
      setBar("bar-disk", "val-disk", diskPct, `${s.diskFree} GB liberi`);
      const batBox = document.getElementById("stat-battery");
      if (typeof s.battery === "number" && s.battery >= 0) {
        batBox.hidden = false;
        setBar("bar-bat", "val-bat", s.battery, s.battery + "%");
      } else {
        batBox.hidden = true;
      }
    } catch (e) { /* il server puo essere momentaneamente occupato */ }
  }
  pollStats();
  setInterval(pollStats, 6000);

  async function loadInfo() {
    try {
      const i = await (await fetch("/api/info")).json();
      document.getElementById("brain-mode").textContent =
        i.has_key ? "Gemini · " + i.model : "Locale (nessuna API key)";
    } catch (e) { /* riprovera al prossimo avvio */ }
  }
  loadInfo();

  runBoot();
  connect();
})();
