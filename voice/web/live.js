/* J.A.R.V.I.S. — conversazione dal vivo (Gemini Live), lato HUD.

   La HUD non parla mai con Google: manda il microfono al server di Jarvis
   (frame binari, PCM 16 kHz) e riceve la voce di Jarvis allo stesso modo
   (PCM 24 kHz). I messaggi di controllo viaggiano come JSON sullo stesso
   WebSocket, con il prefisso "live_".

   Ciclo di vita di una conversazione:
     start()  -> "avvio"  (collegamento in corso, il microfono si apre subito
                           e il server tiene da parte i primi secondi)
     server "open"        -> "aperta" (si parla liberamente)
     silenzio prolungato, congedo ("grazie, è tutto"), clic sul microfono
              -> stop()  -> "spenta"
   Le parti di interfaccia (registro, stato, didascalia) restano in app.js:
   qui c'è solo il motore, collegato tramite i ganci passati a init(). */
(() => {
  "use strict";

  let h = null;                       // ganci verso app.js
  const cfg = { silenzioMs: 20000, interruzioni: true };
  let stato = "spenta";               // spenta | avvio | aperta
  let sid = 0;                        // numero della conversazione corrente
  let pronto = null;                  // preparazione dei contesti audio

  let playCtx = null, playNode = null, voiceAnalyser = null;
  let capCtx = null, capNode = null, capSrc = null, capSink = null;
  let micAnalyser = null, micStream = null;

  let suona = false;                  // la voce di Jarvis sta uscendo dalle casse
  let fermoTimer = null;
  let strumenti = 0;                  // strumenti in esecuzione sul server
  let pilota = false;                 // il pilota automatico sta usando il PC
  let congedo = false;                // il modello ha chiuso: si esce dopo il saluto
  let ultimaAttivita = 0;
  let timer = null;

  function segnaAttivita() { ultimaAttivita = Date.now(); }

  /* ---------- Audio ---------- */
  function preparaAudio() {
    if (!pronto) {
      pronto = (async () => {
        const AC = window.AudioContext || window.webkitAudioContext;
        // Riproduzione alla frequenza nativa di Gemini (24 kHz): il browser
        // converte da solo verso la scheda audio, niente ricampionamento a mano.
        playCtx = new AC({ sampleRate: 24000, latencyHint: "interactive" });
        await playCtx.audioWorklet.addModule("live-worklet.js");
        playNode = new AudioWorkletNode(playCtx, "jarvis-player",
          { numberOfInputs: 0, numberOfOutputs: 1, outputChannelCount: [1] });
        // L'analizzatore fa pulsare il reattore sulla voce vera.
        voiceAnalyser = playCtx.createAnalyser();
        voiceAnalyser.fftSize = 128;
        voiceAnalyser.smoothingTimeConstant = 0.6;
        playNode.connect(voiceAnalyser);
        voiceAnalyser.connect(playCtx.destination);
        playNode.port.onmessage = (e) => onPlayer(e.data);
        // Cattura a 16 kHz, il formato che vuole Gemini. Se il browser non
        // accetta la frequenza, il processore decima da solo.
        try { capCtx = new AC({ sampleRate: 16000 }); } catch (e) { capCtx = new AC(); }
        await capCtx.audioWorklet.addModule("live-worklet.js");
      })();
      pronto.catch(() => { pronto = null; });   // al prossimo tentativo si riprova
    }
    return pronto;
  }

  function riprendiContesti() {
    // Senza un gesto del signore il browser tiene l'audio sospeso: resume()
    // non va atteso, perché in quel caso resterebbe appeso per sempre.
    if (playCtx && playCtx.state !== "running") playCtx.resume().catch(() => {});
    if (capCtx && capCtx.state !== "running") capCtx.resume().catch(() => {});
  }

  async function apriMicrofono() {
    if (micStream) return;
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        // L'eliminazione dell'eco del browser toglie dal microfono la voce di
        // Jarvis che esce dalle casse: senza, Jarvis si sentirebbe parlare e
        // si interromperebbe da solo.
        echoCancellation: true, noiseSuppression: true, autoGainControl: true,
        channelCount: 1,
      },
    });
    if (stato === "spenta") {          // chiusa mentre il browser chiedeva il permesso
      stream.getTracks().forEach((t) => t.stop());
      return;
    }
    micStream = stream;
    capSrc = capCtx.createMediaStreamSource(stream);
    capNode = new AudioWorkletNode(capCtx, "jarvis-mic",
      { numberOfInputs: 1, numberOfOutputs: 1, outputChannelCount: [1] });
    capNode.port.onmessage = (e) => {
      if (stato !== "spenta") h.sendBinary(e.data);
    };
    // Un nodo che non arriva alle casse potrebbe non essere mai eseguito: lo
    // si collega attraverso un volume a zero, così gira di sicuro e non si sente.
    capSink = capCtx.createGain();
    capSink.gain.value = 0;
    micAnalyser = capCtx.createAnalyser();
    micAnalyser.fftSize = 128;
    capSrc.connect(capNode);
    capNode.connect(capSink);
    capSink.connect(capCtx.destination);
    capSrc.connect(micAnalyser);
    aggiornaMuto();
  }

  function chiudiMicrofono() {
    try { if (capSrc) capSrc.disconnect(); } catch (e) { /* già staccato */ }
    try { if (capNode) { capNode.port.onmessage = null; capNode.disconnect(); } } catch (e) { /* idem */ }
    try { if (capSink) capSink.disconnect(); } catch (e) { /* idem */ }
    if (micStream) micStream.getTracks().forEach((t) => t.stop());
    micStream = capSrc = capNode = capSink = micAnalyser = null;
  }

  function aggiornaMuto() {
    // Con le interruzioni spente il microfono tace mentre Jarvis parla.
    if (capNode) capNode.port.postMessage({ muto: !cfg.interruzioni && suona });
  }

  function svuota() {
    if (playNode) playNode.port.postMessage("svuota");
  }

  function onPlayer(msg) {
    if (msg === "parla") {
      clearTimeout(fermoTimer);
      if (!suona) {
        suona = true;
        aggiornaMuto();
        h.onSpeaking(true);
      }
      segnaAttivita();
    } else if (msg === "fermo") {
      clearTimeout(fermoTimer);
      // Fra due pezzi di audio in arrivo dalla rete può aprirsi un buco di
      // pochi millisecondi: non è la fine della frase.
      fermoTimer = setTimeout(() => {
        if (!suona) return;
        suona = false;
        aggiornaMuto();
        segnaAttivita();
        h.onSpeaking(false);
        if (congedo) stop("congedo");
      }, 300);
    }
  }

  /* ---------- Ciclo di vita ---------- */
  async function start(testo) {
    if (stato !== "spenta") {
      if (testo) {
        h.send({ type: "live_text", text: testo, sid });
        segnaAttivita();
      }
      return;
    }
    stato = "avvio";
    sid += 1;
    congedo = false;
    strumenti = 0;
    pilota = false;
    // Il server comincia a collegarsi SUBITO, mentre qui si prepara l'audio:
    // i due tempi si sovrappongono invece di sommarsi.
    h.send({ type: "live_start", text: testo || "", voice: h.voce(), sid });
    h.onState("avvio");
    segnaAttivita();
    avviaTimer();
    try {
      await preparaAudio();
    } catch (e) {
      if (stato === "spenta") return;
      stop("audio");
      h.onError({ message: "Il browser non riesce ad avviare l'audio dal vivo: torno alla modalità classica.",
                  kind: "audio" });
      return;
    }
    riprendiContesti();
    try {
      await apriMicrofono();
    } catch (e) {
      // Microfono negato o occupato: si può ancora scrivere, e Jarvis
      // risponde comunque a voce.
      if (stato !== "spenta") h.onMicError(e);
    }
  }

  function stop(motivo) {
    if (stato === "spenta") return;
    h.send({ type: "live_stop", reason: motivo || "HUD", sid });
    chiudiLocale(motivo || "HUD");
  }

  function chiudiLocale(motivo) {
    if (stato === "spenta") return;
    stato = "spenta";
    congedo = false;
    strumenti = 0;
    if (pilota) {
      pilota = false;
      h.onPilota(false);
    }
    clearInterval(timer);
    timer = null;
    clearTimeout(fermoTimer);
    svuota();
    if (suona) {
      suona = false;
      h.onSpeaking(false);
    }
    chiudiMicrofono();
    h.onState("spenta", motivo);
  }

  function avviaTimer() {
    clearInterval(timer);
    timer = setInterval(() => {
      if (stato === "spenta") return;
      // Mentre parla o lavora non è silenzio.
      if (suona || strumenti > 0) {
        segnaAttivita();
        return;
      }
      const fermo = Date.now() - ultimaAttivita;
      if (stato === "avvio") {
        // Il server ritenta da solo per qualche secondo se la rete inciampa.
        if (fermo > 15000) {
          stop("collegamento lento");
          h.onError({ message: "Il collegamento dal vivo non risponde: torno alla modalità classica.",
                      kind: "rete" });
        }
        return;
      }
      if (fermo > cfg.silenzioMs) stop("silenzio");
    }, 500);
  }

  /* ---------- Messaggi dal server ---------- */
  function onMessage(m) {
    // Un messaggio ritardato di una conversazione già chiusa non deve toccare
    // quella nuova.
    if (m.sid !== undefined && m.sid !== sid) return;
    if (stato === "spenta") return;
    switch (m.type) {
      case "live_state":
        if (m.state === "open") {
          stato = "aperta";
          segnaAttivita();
          h.onState("aperta", m);
        } else if (m.state === "closed") {
          chiudiLocale(m.reason || "chiusa");
        } else if (m.state === "error") {
          chiudiLocale("errore");
          h.onError(m);
        }
        break;
      case "live_user":
        segnaAttivita();
        h.onUserText(m.text || "");
        break;
      case "live_jarvis":
        segnaAttivita();
        h.onJarvisText(m.text || "");
        break;
      case "live_interrupt":
        svuota();
        h.onInterrupted();
        break;
      case "live_turn":
        segnaAttivita();
        h.onTurn();
        break;
      case "live_tool":
        segnaAttivita();
        if (m.stato === "inizio") {
          strumenti += 1;
          if (m.nome === "use_computer" && !pilota) {
            pilota = true;
            h.onPilota(true);
          }
        } else {
          strumenti = Math.max(0, strumenti - 1);
          if (m.nome === "use_computer" && pilota) {
            pilota = false;
            h.onPilota(false);
          }
        }
        h.onTool(m.stato === "inizio", m.nome);
        break;
      case "live_end":
        // Il signore ha congedato Jarvis: si chiude appena finisce il saluto.
        congedo = true;
        setTimeout(() => { if (congedo && !suona) stop("congedo"); }, 600);
        break;
      default:
        break;
    }
  }

  function onAudio(buf) {
    if (stato === "spenta" || !playNode) return;
    segnaAttivita();
    playNode.port.postMessage(buf, [buf]);
  }

  window.JarvisLive = {
    init(ganci) { h = ganci; },
    configura(info) {
      if (info && info.live_silenzio_s) cfg.silenzioMs = info.live_silenzio_s * 1000;
      if (info && typeof info.live_interruzioni === "boolean") cfg.interruzioni = info.live_interruzioni;
    },
    /* Al primo gesto del signore: prepara i contesti audio, così una
       conversazione aperta poi a voce («Jarvis») non resta muta. */
    sblocca() {
      preparaAudio().then(riprendiContesti).catch(() => {});
    },
    start,
    stop,
    onMessage,
    onAudio,
    disconnesso() { chiudiLocale("connessione persa"); },
    isActive: () => stato !== "spenta",
    isOpen: () => stato === "aperta",
    isPlaying: () => suona,
    pilotaInCorso: () => pilota,
    analysers: () => ({ voice: voiceAnalyser, mic: micAnalyser }),
  };
})();
