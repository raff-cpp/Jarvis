/* J.A.R.V.I.S. — processori audio della conversazione dal vivo.

   Girano nel thread audio del browser (AudioWorklet), non in quello della
   pagina: così la voce non scatta quando la HUD è impegnata a disegnare.

   jarvis-mic     microfono -> PCM 16 bit, 16 kHz, mono: il formato che vuole
                  Gemini Live. Spedisce blocchi da 64 ms alla pagina, che li
                  gira al server.
   jarvis-player  PCM 16 bit, 24 kHz in arrivo dal server -> casse. Tiene una
                  coda e la svuota all'istante quando il signore interrompe. */

const BLOCCO = 1024;            // campioni a 16 kHz = 64 ms

class JarvisMic extends AudioWorkletProcessor {
  constructor() {
    super();
    // Il contesto dovrebbe già girare a 16 kHz (il browser ricampiona il
    // microfono). Se il browser non l'ha concesso, si decima qui: media dei
    // campioni che cadono in ciascuna posizione d'uscita, che fa anche da
    // filtro contro l'aliasing.
    this.passo = sampleRate / 16000;
    this.pos = 0;
    this.somma = 0;
    this.n = 0;
    this.out = new Int16Array(BLOCCO);
    this.k = 0;
    this.muto = false;
    this.port.onmessage = (e) => {
      if (e.data && typeof e.data.muto === "boolean") this.muto = e.data.muto;
    };
  }

  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (!ch) return true;
    for (let i = 0; i < ch.length; i++) {
      this.somma += ch[i];
      this.n++;
      this.pos += 1;
      if (this.pos < this.passo) continue;
      this.pos -= this.passo;
      // Muto = silenzio vero, non assenza di audio: il rilevamento del
      // parlato di Google ha bisogno di sentire che il signore tace.
      let v = this.muto ? 0 : this.somma / this.n;
      this.somma = 0;
      this.n = 0;
      v = v > 1 ? 1 : (v < -1 ? -1 : v);
      this.out[this.k++] = v < 0 ? v * 0x8000 : v * 0x7fff;
      if (this.k === BLOCCO) {
        this.port.postMessage(this.out.buffer, [this.out.buffer]);
        this.out = new Int16Array(BLOCCO);
        this.k = 0;
      }
    }
    return true;
  }
}

class JarvisPlayer extends AudioWorkletProcessor {
  constructor() {
    super();
    this.coda = [];
    this.cur = null;
    this.idx = 0;
    this.attivo = false;
    this.port.onmessage = (e) => {
      if (e.data === "svuota") {
        this.coda = [];
        this.cur = null;
        this.idx = 0;
        this.fermo();
        return;
      }
      const pcm = new Int16Array(e.data);
      const f = new Float32Array(pcm.length);
      for (let i = 0; i < pcm.length; i++) f[i] = pcm[i] / 32768;
      this.coda.push(f);
    };
  }

  fermo() {
    if (this.attivo) {
      this.attivo = false;
      this.port.postMessage("fermo");
    }
  }

  process(inputs, outputs) {
    const out = outputs[0][0];
    let i = 0;
    while (i < out.length) {
      if (!this.cur) {
        if (!this.coda.length) break;
        this.cur = this.coda.shift();
        this.idx = 0;
        if (!this.attivo) {
          this.attivo = true;
          this.port.postMessage("parla");
        }
      }
      const n = Math.min(out.length - i, this.cur.length - this.idx);
      out.set(this.cur.subarray(this.idx, this.idx + n), i);
      i += n;
      this.idx += n;
      if (this.idx >= this.cur.length) this.cur = null;
    }
    for (; i < out.length; i++) out[i] = 0;
    if (!this.cur && !this.coda.length) this.fermo();
    return true;
  }
}

registerProcessor("jarvis-mic", JarvisMic);
registerProcessor("jarvis-player", JarvisPlayer);
