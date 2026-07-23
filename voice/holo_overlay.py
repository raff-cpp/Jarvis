"""J.A.R.V.I.S. — overlay olografico per le finestre REALI.

Un livello trasparente, sempre in primo piano, che copre l'intero desktop
(tutti i monitor). Ci si vede attraverso il desktro vero: sopra ogni finestra
puntata compare una cornice olografica, e con il "pinch" la afferri e la sposti
con la mano — anche da un monitor all'altro.

Il tracciamento della mano NON sta qui: arriva dal browser (MediaPipe, gia'
tarato) attraverso il server, che inoltra la posizione a questo processo via
stdin (una riga JSON per fotogramma: {"x":..,"y":..,"pinch":..} con x,y in
[0,1] sul desktop virtuale). Cosi' non serve reintrodurre una libreria di
visione in Python.

Modalita':
  (nessun argomento)  overlay dal vivo, pilotato dalla mano via stdin
  --mouse             pilotato dal MOUSE (SHIFT tenuto = pinch): per provarlo
                      subito senza webcam
  --selftest          collauda il solo controller (afferra/sposta) e stampa
                      l'esito, senza aprire l'overlay

Solo librerie standard: ctypes + tkinter. Nessuna dipendenza esterna.
"""
import ctypes
import json
import sys
import threading
import time
from ctypes import wintypes

user32 = ctypes.windll.user32
try:
    user32.SetProcessDPIAware()
except Exception:
    pass

GA_ROOT = 2
SWP_NOSIZE, SWP_NOMOVE, SWP_NOZORDER, SWP_NOACTIVATE = 0x0001, 0x0002, 0x0004, 0x0010
GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
SW_RESTORE = 9
VK_SHIFT = 0x10
VK_ESCAPE = 0x1B

COLORE = "#35e0ff"          # ciano di Jarvis
CHIAVE = "#080608"          # colore reso trasparente (quasi nero, non usato altrove)

# Classi da NON toccare mai: shell, barra applicazioni, sfondo del desktop.
CLASSI_VIETATE = {"Shell_TrayWnd", "Shell_SecondaryTrayWnd", "Progman", "WorkerW",
                  "NotifyIconOverflowWindow", "Windows.UI.Core.CoreWindow",
                  "ForegroundStaging", "XamlExplorerHostIslandWindow"}


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


def _testo(hwnd):
    buf = ctypes.create_unicode_buffer(512)
    user32.GetWindowTextW(hwnd, buf, 512)
    return buf.value


def _classe(hwnd):
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def _rect(hwnd):
    r = RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return r


def _metrics_desktop_virtuale():
    """Origine e dimensione del desktop che unisce tutti i monitor."""
    x = user32.GetSystemMetrics(76)   # SM_XVIRTUALSCREEN
    y = user32.GetSystemMetrics(77)   # SM_YVIRTUALSCREEN
    w = user32.GetSystemMetrics(78)   # SM_CXVIRTUALSCREEN
    h = user32.GetSystemMetrics(79)   # SM_CYVIRTUALSCREEN
    return x, y, w, h


class Gestore:
    """Il cuore: dato un punto e lo stato del pinch, afferra la finestra sotto,
    la trascina finche' il pinch resta chiuso, la rilascia quando si apre.

    Separato apposta dalla grafica, cosi' e' collaudabile da solo."""

    def __init__(self):
        self.presa = None          # hwnd afferrato
        self.offx = self.offy = 0  # scarto punto-angolo al momento della presa
        self.pinch_prec = False
        self.escluso = 0           # hwnd dell'overlay stesso: mai afferrarlo
        self.sotto = None          # finestra attualmente puntata (per la cornice)

    def _radice_a(self, x, y):
        h = user32.WindowFromPoint(POINT(int(x), int(y)))
        if not h:
            return 0
        return user32.GetAncestor(h, GA_ROOT) or h

    def movibile(self, hwnd):
        if not hwnd or hwnd == self.escluso:
            return False
        if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
            return False
        if not _testo(hwnd):
            return False
        if _classe(hwnd) in CLASSI_VIETATE:
            return False
        return True

    def punta(self, x, y, pinch):
        """x,y in pixel del desktop virtuale. Ritorna l'hwnd da evidenziare."""
        x, y = int(x), int(y)
        giu = pinch and not self.pinch_prec
        su = (not pinch) and self.pinch_prec
        self.pinch_prec = pinch

        if giu:
            h = self._radice_a(x, y)
            if self.movibile(h):
                # se e' massimizzata, prima la "stacca" cosi' si puo' spostare
                if _e_massimizzata(h):
                    user32.ShowWindow(h, SW_RESTORE)
                r = _rect(h)
                self.presa = h
                self.offx = x - r.left
                self.offy = y - r.top

        if pinch and self.presa:
            user32.SetWindowPos(self.presa, 0, x - self.offx, y - self.offy,
                                0, 0, SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)

        if su:
            self.presa = None

        if not self.presa:
            h = self._radice_a(x, y)
            self.sotto = h if self.movibile(h) else None
        return self.presa or self.sotto


def _e_massimizzata(hwnd):
    class WP(ctypes.Structure):
        _fields_ = [("length", wintypes.UINT), ("flags", wintypes.UINT),
                    ("showCmd", wintypes.UINT),
                    ("ptMin", POINT), ("ptMax", POINT), ("rcNormal", RECT)]
    wp = WP()
    wp.length = ctypes.sizeof(WP)
    user32.GetWindowPlacement(hwnd, ctypes.byref(wp))
    return wp.showCmd == 3   # SW_SHOWMAXIMIZED


# ===================== overlay grafico (tkinter) =====================
def esegui_overlay(sorgente_mouse=False):
    import tkinter as tk

    vx, vy, vw, vh = _metrics_desktop_virtuale()
    gest = Gestore()

    root = tk.Tk()
    root.overrideredirect(True)
    root.geometry(f"{vw}x{vh}+{vx}+{vy}")
    root.attributes("-topmost", True)
    root.config(bg=CHIAVE)
    root.attributes("-transparentcolor", CHIAVE)

    cv = tk.Canvas(root, width=vw, height=vh, bg=CHIAVE, highlightthickness=0)
    cv.pack(fill="both", expand=True)

    # HWND dell'overlay: rendilo "attraversabile" (click-through) e nascondilo
    # dall'alt-tab, cosi' WindowFromPoint vede le finestre SOTTO di lui.
    root.update_idletasks()
    wid = root.winfo_id()
    parent = user32.GetParent(wid)
    hwnd_overlay = parent if parent else wid
    gest.escluso = hwnd_overlay
    stile = user32.GetWindowLongW(hwnd_overlay, GWL_EXSTYLE)
    user32.SetWindowLongW(hwnd_overlay, GWL_EXSTYLE,
                          stile | WS_EX_LAYERED | WS_EX_TRANSPARENT
                          | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE)

    # zona ESCI, in coordinate LOCALI dell'overlay (angolo alto-destra)
    esci_l, esci_t, esci_r, esci_b = vw - 150, 24, vw - 40, 66

    stato = {"x": vx + vw / 2, "y": vy + vh / 2, "pinch": False, "vivo": True}

    def leggi_stdin():
        for riga in sys.stdin:
            riga = riga.strip()
            if not riga:
                continue
            try:
                m = json.loads(riga)
            except ValueError:
                continue
            if m.get("cmd") == "stop":
                stato["vivo"] = False
                return
            # x,y normalizzati [0,1] sul desktro virtuale
            stato["x"] = vx + float(m.get("x", 0.5)) * vw
            stato["y"] = vy + float(m.get("y", 0.5)) * vh
            stato["pinch"] = bool(m.get("pinch", False))
        # stdin chiuso (il server e' morto): chiudi anche l'overlay, non restare
        # orfano e topmost sullo schermo.
        stato["vivo"] = False

    if not sorgente_mouse:
        threading.Thread(target=leggi_stdin, daemon=True).start()

    def esci(motivo="stop"):
        stato["vivo"] = False
        print("STOPPED", flush=True)
        try:
            root.destroy()
        except Exception:
            pass

    def ciclo():
        if not stato["vivo"]:
            esci()
            return
        # ESC globale = uscita
        if user32.GetAsyncKeyState(VK_ESCAPE) & 0x8000:
            esci("esc")
            return

        if sorgente_mouse:
            p = POINT()
            user32.GetCursorPos(ctypes.byref(p))
            stato["x"], stato["y"] = p.x, p.y
            stato["pinch"] = bool(user32.GetAsyncKeyState(VK_SHIFT) & 0x8000)

        sx, sy = stato["x"], stato["y"]
        pinch = stato["pinch"]

        # zona ESCI: un pinch qui chiude
        lx, ly = sx - vx, sy - vy
        in_esci = esci_l <= lx <= esci_r and esci_t <= ly <= esci_b
        if in_esci and pinch and not gest.pinch_prec:
            esci("zona")
            return

        target = None if in_esci else gest.punta(sx, sy, pinch)
        disegna(cv, vx, vy, vw, vh, lx, ly, pinch, target,
                (esci_l, esci_t, esci_r, esci_b), in_esci)
        root.after(16, ciclo)

    root.after(16, ciclo)
    root.mainloop()


def disegna(cv, vx, vy, vw, vh, lx, ly, pinch, target, esci_box, in_esci):
    cv.delete("all")
    # cornice attorno alla finestra puntata/afferrata
    if target:
        r = _rect(target)
        x1, y1, x2, y2 = r.left - vx, r.top - vy, r.right - vx, r.bottom - vy
        col = COLORE if not pinch else "#eafcff"
        cv.create_rectangle(x1, y1, x2, y2, outline=col, width=3)
        for (cx, cy) in ((x1, y1), (x2, y1), (x1, y2), (x2, y2)):
            cv.create_rectangle(cx - 8, cy - 8, cx + 8, cy + 8, outline=col, width=3)
        tit = _testo(target)[:60]
        if tit:
            cv.create_text(x1 + 6, y1 - 12, text=tit, anchor="w", fill=COLORE,
                           font=("Consolas", 10))

    # cursore
    rr = 10 if pinch else 16
    fill = COLORE if pinch else ""
    cv.create_oval(lx - rr, ly - rr, lx + rr, ly + rr, outline=COLORE, width=2, fill=fill)
    cv.create_oval(lx - 2, ly - 2, lx + 2, ly + 2, outline=COLORE, fill=COLORE)

    # tasto ESCI
    el, et, er, eb = esci_box
    cv.create_rectangle(el, et, er, eb, outline=COLORE, width=2,
                        fill=("#123" if in_esci else CHIAVE))
    cv.create_text((el + er) / 2, (et + eb) / 2, text="ESCI", fill=COLORE,
                   font=("Consolas", 12, "bold"))

    # suggerimento in basso
    cv.create_text(vw / 2, vh - 26,
                   text="MANO = PUNTA · PINCH = AFFERRA E SPOSTA · «ESCI» o ESC per uscire",
                   fill=COLORE, font=("Consolas", 11))


# ===================== collaudo del solo controller =====================
def selftest():
    import subprocess
    titolo = "JARVIS-OVERLAY-SELFTEST-Q"
    child = subprocess.Popen([sys.executable, "-c",
        "import tkinter as tk;r=tk.Tk();r.title('%s');r.geometry('460x340+150+150');"
        "r.attributes('-topmost',True);r.lift();r.mainloop()" % titolo])
    time.sleep(1.5)
    try:
        EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        found = []

        def cb(h, l):
            if user32.IsWindowVisible(h) and titolo in _testo(h):
                found.append(h)
                return False
            return True
        user32.EnumWindows(EnumProc(cb), 0)
        if not found:
            print("SELFTEST FAIL: finestra non trovata")
            return 1
        h = found[0]
        r0 = _rect(h)
        cx, cy = (r0.left + r0.right) // 2, (r0.top + r0.bottom) // 2

        g = Gestore()
        g.punta(cx, cy, True)                 # afferra
        if g.presa != h:
            # Sotto il punto c'era un'altra finestra (es. la HUD a schermo intero):
            # rilascio e mi fermo, senza spostare nulla di estraneo.
            g.punta(cx, cy, False)
            print("SELFTEST SALTATO: la finestra di prova era coperta (presa=%s)" % g.presa)
            return 2
        g.punta(cx + 250, cy + 150, True)     # trascina
        r1 = _rect(h)
        g.punta(cx + 250, cy + 150, False)    # rilascia
        # dopo il rilascio, muovere il punto NON deve piu' spostare la finestra
        g.punta(cx + 600, cy + 400, False)
        r2 = _rect(h)

        dx, dy = r1.left - r0.left, r1.top - r0.top
        ok_move = abs(dx - 250) <= 3 and abs(dy - 150) <= 3
        ok_release = (r2.left == r1.left and r2.top == r1.top)
        print(f"trascinato dx,dy={dx},{dy} (atteso 250,150) -> {'PASS' if ok_move else 'FAIL'}")
        print(f"dopo rilascio fermo -> {'PASS' if ok_release else 'FAIL'}")
        print("SELFTEST " + ("TUTTO-OK" if (ok_move and ok_release) else "CI-SONO-ERRORI"))
        return 0 if (ok_move and ok_release) else 1
    finally:
        try:
            child.terminate()
        except Exception:
            pass


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    esegui_overlay(sorgente_mouse=("--mouse" in sys.argv))
