"""Osservatore in background: e' cio' che rende Jarvis capace di parlare per primo.

Finora Jarvis era puramente reattivo: rispondeva solo se interrogato. Qui invece
sorveglia poche cose scelte e avvisa a voce quando succedono.

Due principi guidano il modulo:

1. NON deve mai far cadere il server. Ogni controllo e' avvolto in un
   try/except: un errore in un controllo non ferma il ciclo ne' gli altri.
2. NON deve consumare quota. Le condizioni sono regole locali e le frasi sono
   fisse, quindi nessuna chiamata al modello e voce presa dalla cache.
"""
import os
import threading
import time

import tools

# Estensioni dei file ancora in scaricamento: vanno ignorate finche' non
# spariscono, altrimenti annunceremmo download a meta'.
_PARZIALI = (".crdownload", ".part", ".tmp", ".download", ".opdownload", ".partial")

INTERVALLO_S = 15          # ogni quanto guardare la cartella dei download
BATTERIA_INTERVALLO_S = 60
BATTERIA_SOGLIA = 15       # percentuale sotto cui avvisare


class Watcher:
    """Sorveglia download e batteria, e annuncia tramite la callback."""

    def __init__(self, annuncia, cartella_download=None):
        self.annuncia = annuncia
        self.cartella = cartella_download or os.path.join(
            os.path.expanduser("~"), "Downloads")
        self._stop = threading.Event()
        self._thread = None
        # Fotografia iniziale: cosi' al primo giro non annuncia i file che
        # c'erano gia' da settimane.
        self._visti = self._istantanea()
        self._batteria_avvisata = False
        self._ultimo_controllo_batteria = 0.0

    # ---- download -----------------------------------------------------------
    def _istantanea(self):
        try:
            return {n for n in os.listdir(self.cartella)}
        except OSError:
            return set()

    def _controlla_download(self):
        attuali = self._istantanea()
        nuovi = attuali - self._visti
        # Aggiorniamo subito: anche i file scartati non vanno riesaminati ogni giro.
        self._visti = attuali
        for nome in sorted(nuovi):
            if nome.lower().endswith(_PARZIALI):
                # Ancora in corso: lo togliamo dai visti cosi' lo riconosceremo
                # quando avra' il nome definitivo.
                self._visti.discard(nome)
                continue
            percorso = os.path.join(self.cartella, nome)
            try:
                if os.path.isdir(percorso) or os.path.getsize(percorso) == 0:
                    continue
            except OSError:
                continue
            pulito = os.path.splitext(nome)[0]
            if len(pulito) > 45:
                pulito = pulito[:45] + "…"
            self.annuncia(f"Download completato: {pulito}.")

    # ---- batteria -----------------------------------------------------------
    def _controlla_batteria(self, adesso):
        if adesso - self._ultimo_controllo_batteria < BATTERIA_INTERVALLO_S:
            return
        self._ultimo_controllo_batteria = adesso
        stato = tools.system_status_brief()
        livello = stato.get("battery", -1)
        if not isinstance(livello, int) or livello < 0:
            return                       # fisso, senza batteria: niente da fare
        if livello <= BATTERIA_SOGLIA and not self._batteria_avvisata:
            self._batteria_avvisata = True
            self.annuncia(f"Batteria al {livello} per cento, signore.")
        elif livello > BATTERIA_SOGLIA + 10:
            # Isteresi: si riarma solo dopo essere risalita, non appena
            # oscilla di un punto attorno alla soglia.
            self._batteria_avvisata = False

    # ---- ciclo --------------------------------------------------------------
    def _ciclo(self):
        while not self._stop.is_set():
            adesso = time.time()
            for controllo in (self._controlla_download,
                              lambda: self._controlla_batteria(adesso)):
                try:
                    controllo()
                except Exception as e:
                    # Un controllo rotto non deve fermare gli altri ne' il ciclo.
                    print(f"[jarvis] osservatore, controllo fallito: {e}")
            self._stop.wait(INTERVALLO_S)

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._ciclo, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
