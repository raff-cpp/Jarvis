"""Strumenti di Jarvis: controllo completo del PC Windows.

Ogni funzione ritorna un dict semplice (status + dati) che viene serializzato e
passato al modello come risultato del tool. La sicurezza e centralizzata qui: i
comandi potenzialmente distruttivi richiedono conferma esplicita dell'utente.

Nessuna chiave API viene mai toccata da questo modulo.
"""
import base64
import ctypes
import datetime
import json
import os
import re
import subprocess
import threading
import time
import uuid

# Radice del workspace Jarvis (una cartella sopra /voice)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_tmp")
MEMORY_FILE = os.path.join(ROOT, "memoria.json")

# Pattern di comandi distruttivi / irreversibili -> richiedono confirm=True
_DESTRUCTIVE = re.compile(
    r"(Remove-Item|\brm\b|\bdel\b|\berase\b|rmdir|\brd\b|Clear-Content|Clear-Item|"
    r"Format-Volume|\bFormat\b|Clear-Disk|diskpart|"
    r"Stop-Computer|Restart-Computer|\bshutdown\b|logoff|"
    r"reg\s+delete|Remove-ItemProperty|"
    r"Set-ExecutionPolicy|Uninstall-|Stop-Process|Stop-Service|taskkill|"
    r"Disable-|net\s+user|icacls)",
    re.IGNORECASE,
)


# ---- Infrastruttura PowerShell ---------------------------------------------

def _q(value):
    """Cita una stringa come literal PowerShell ('...'), neutralizzando gli apici."""
    return "'" + str(value).replace("'", "''") + "'"


def _clean_stderr(text):
    """Rende leggibile lo stderr di PowerShell.

    Quando stderr e rediretto, PowerShell 5.1 non scrive testo ma un blob CLIXML
    (`#< CLIXML <Objs>...`): illeggibile per il signore e inutile per il modello.
    Qui ne estraiamo i soli messaggi d'errore.
    """
    if not text or "CLIXML" not in text:
        return text
    parti = re.findall(r'<S S="Error">(.*?)</S>', text, re.S)
    if not parti:
        return text
    msg = "".join(parti)
    msg = msg.replace("_x000D_", "").replace("_x000A_", "\n")
    msg = (msg.replace("&lt;", "<").replace("&gt;", ">")
              .replace("&amp;", "&").replace("&quot;", '"').replace("&apos;", "'"))
    # Via le righe di contorno (indicatori di posizione, CategoryInfo...).
    righe = [r.strip() for r in msg.splitlines()
             if r.strip() and not r.strip().startswith(("+", "~"))]
    return "\n".join(righe).strip()


def _run_ps(command, timeout=30):
    """Esegue un comando PowerShell, ritorna exit_code/stdout/stderr.

    Il comando viaggia come -EncodedCommand (UTF-16LE in base64): e l'unico modo
    affidabile per far arrivare a PowerShell accenti e simboli senza che la
    codepage della console (cp1252) li rovini. Allo stesso modo l'output viene
    letto come byte e decodificato in UTF-8: con text=True Python userebbe la
    codepage locale e andrebbe in UnicodeDecodeError sui caratteri accentati.
    """
    wrapped = ("[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;"
               "$OutputEncoding=[System.Text.Encoding]::UTF8;" + command)
    encoded = base64.b64encode(wrapped.encode("utf-16-le")).decode("ascii")
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive",
             "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded],
            capture_output=True, timeout=timeout,       # byte grezzi, decodifichiamo noi
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        out = (proc.stdout or b"").decode("utf-8", "replace")
        err = _clean_stderr((proc.stderr or b"").decode("utf-8", "replace"))
        return {
            "exit_code": proc.returncode,
            "stdout": out.strip()[:6000],
            "stderr": err.strip()[:2000],
        }
    except subprocess.TimeoutExpired:
        return {"exit_code": -1, "stdout": "", "stderr": f"Timeout dopo {timeout}s"}
    except Exception as e:
        return {"exit_code": -1, "stdout": "", "stderr": str(e)}


def _purge_tmp(max_age_s=3600):
    """Elimina gli script temporanei gia eseguiti."""
    if not os.path.isdir(TMP):
        return
    now = time.time()
    for name in os.listdir(TMP):
        path = os.path.join(TMP, name)
        try:
            if now - os.path.getmtime(path) > max_age_s:
                os.remove(path)
        except OSError:
            pass


def _run_ps_detached(script):
    """Lancia uno script PowerShell in background senza attendere (per toast, ecc.)."""
    os.makedirs(TMP, exist_ok=True)
    _purge_tmp()
    path = os.path.join(TMP, f"j_{uuid.uuid4().hex[:8]}.ps1")
    # BOM obbligatorio: senza, PowerShell 5.1 legge il file come ANSI e
    # storpia accenti e simboli nelle notifiche.
    with open(path, "w", encoding="utf-8-sig") as f:
        f.write(script)
    subprocess.Popen(
        ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-WindowStyle", "Hidden", "-File", path],
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return path


_FG_TITLE_PS = '''Add-Type @"
using System;using System.Runtime.InteropServices;using System.Text;
public class JvFg {
  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
  [DllImport("user32.dll")] public static extern int GetWindowText(IntPtr h, StringBuilder s, int n);
  public static string Title(){ var sb=new StringBuilder(300); GetWindowText(GetForegroundWindow(), sb, 300); return sb.ToString(); }
}
"@ -ErrorAction SilentlyContinue
[JvFg]::Title()'''


def foreground_window():
    """Titolo della finestra in primo piano (quella che ricevera i tasti)."""
    r = _run_ps(_FG_TITLE_PS, timeout=30)
    return r["stdout"].strip()


def _focus_and_verify(match, attempts=3, pause=1.2):
    """Porta avanti una finestra e CONFERMA che sia davvero in primo piano.

    SendKeys scrive nella finestra attiva qualunque essa sia: senza questa
    verifica un messaggio potrebbe finire nell'editor di codice del signore.
    """
    needles = [m.lower() for m in (match if isinstance(match, (list, tuple)) else [match])]
    for i in range(attempts):
        title = foreground_window()
        if any(n in title.lower() for n in needles):
            return True, title
        # Prova a portarla in primo piano e riguarda.
        _run_ps("$w=New-Object -ComObject WScript.Shell;"
                f"$null=$w.AppActivate({_q(needles[0])})", timeout=20)
        time.sleep(pause)
    return False, foreground_window()


# ---- PowerShell persistente (per i gesti, che devono essere immediati) ------

# Avviare un processo PowerShell costa ~0.35s: accettabile per un comando
# vocale, inaccettabile per un pinch che aggiorna il volume dieci volte al
# secondo. Qui teniamo vivo un solo processo con i tipi gia compilati: da li
# in poi ogni comando costa pochi millisecondi.
_PS_PRELUDE = r'''
Add-Type -AssemblyName System.Windows.Forms -ErrorAction SilentlyContinue
Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
[Guid("5CDF2C82-841E-4546-9722-0CF74078229A"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IAudioEndpointVolume {
  int _a(); int _b(); int _c(); int _d();
  int SetMasterVolumeLevelScalar(float fLevel, Guid ctx);
  int _e();
  int GetMasterVolumeLevelScalar(out float pfLevel);
  int _f(); int _g(); int _h(); int _i();
  int SetMute([MarshalAs(UnmanagedType.Bool)] bool bMute, Guid ctx);
  int GetMute(out bool pbMute);
}
[Guid("D666063F-1587-4E43-81F1-B948E807363F"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IMMDevice { int Activate(ref Guid id, int ctx, IntPtr p, out IAudioEndpointVolume v); }
[Guid("A95664D2-9614-4F35-A746-DE8DB63617E6"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IMMDeviceEnumerator { int _a(); int GetDefaultAudioEndpoint(int flow, int role, out IMMDevice dev); }
[ComImport, Guid("BCDE0395-E52F-467C-8E3D-C4579291692E")] class MMDeviceEnumeratorComObject { }
public class JvAudio {
  static IAudioEndpointVolume V() {
    var e = new MMDeviceEnumeratorComObject() as IMMDeviceEnumerator;
    IMMDevice d = null; Marshal.ThrowExceptionForHR(e.GetDefaultAudioEndpoint(0, 1, out d));
    IAudioEndpointVolume v = null; var g = typeof(IAudioEndpointVolume).GUID;
    Marshal.ThrowExceptionForHR(d.Activate(ref g, 23, IntPtr.Zero, out v)); return v;
  }
  public static int Get(){ float v; V().GetMasterVolumeLevelScalar(out v); return (int)Math.Round(v*100); }
  public static void Set(int p){ V().SetMasterVolumeLevelScalar(Math.Max(0,Math.Min(100,p))/100f, Guid.Empty); }
  public static void Mute(bool m){ V().SetMute(m, Guid.Empty); }
  public static bool Muted(){ bool m; V().GetMute(out m); return m; }
  public static void ToggleMute(){ Mute(!Muted()); }
}
public class JvKeys {
  [DllImport("user32.dll")] public static extern void keybd_event(byte vk, byte scan, uint flags, UIntPtr extra);
  // Iniezione a livello di SISTEMA, come una tastiera vera: i tasti
  // multimediali arrivano al lettore anche se il fuoco e' altrove.
  // SendKeys invece consegna solo alla finestra in primo piano.
  public static void Tap(byte vk) {
    keybd_event(vk, 0, 0, UIntPtr.Zero);
    keybd_event(vk, 0, 2, UIntPtr.Zero);   // 2 = KEYEVENTF_KEYUP
  }
}
public class JvWin {
  [StructLayout(LayoutKind.Sequential)] public struct RECT { public int Left, Top, Right, Bottom; }
  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
  [DllImport("user32.dll")] public static extern bool SetWindowPos(IntPtr h, IntPtr a, int x, int y, int cx, int cy, uint f);
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int cmd);
  [DllImport("user32.dll")] public static extern bool IsZoomed(IntPtr h);
  [DllImport("user32.dll")] public static extern int GetWindowText(IntPtr h, System.Text.StringBuilder s, int n);
  public static string Title(){ var sb = new System.Text.StringBuilder(300); GetWindowText(GetForegroundWindow(), sb, 300); return sb.ToString(); }
}
"@ -ErrorAction SilentlyContinue

function Move-JvWindow($dir) {
  $h = [JvWin]::GetForegroundWindow()
  if ($h -eq [IntPtr]::Zero) { return }
  $r = New-Object JvWin+RECT
  [void][JvWin]::GetWindowRect($h, [ref]$r)
  $cx = ($r.Left + $r.Right) / 2
  $screens = @([System.Windows.Forms.Screen]::AllScreens | Sort-Object { $_.Bounds.X })
  if ($screens.Count -lt 2) { return }
  $cur = 0
  for ($i = 0; $i -lt $screens.Count; $i++) {
    $b = $screens[$i].Bounds
    if ($cx -ge $b.X -and $cx -lt ($b.X + $b.Width)) { $cur = $i }
  }
  # 'other' = lo schermo successivo a giro: con due monitor e' semplicemente
  # "l'altro", senza bisogno di indicare una direzione.
  if ($dir -eq 'other') { $t = ($cur + 1) % $screens.Count }
  elseif ($dir -eq 'right') { $t = $cur + 1 }
  else { $t = $cur - 1 }
  if ($t -lt 0 -or $t -ge $screens.Count) { return }
  # Una finestra massimizzata va prima ripristinata, spostata e poi rimassimizzata.
  $max = [JvWin]::IsZoomed($h)
  if ($max) { [void][JvWin]::ShowWindow($h, 9) }
  [void][JvWin]::GetWindowRect($h, [ref]$r)
  $w = $r.Right - $r.Left; $ht = $r.Bottom - $r.Top
  $src = $screens[$cur].WorkingArea; $dst = $screens[$t].WorkingArea
  $relX = ($r.Left - $src.X) / [double]$src.Width
  $relY = ($r.Top - $src.Y) / [double]$src.Height
  $w = [Math]::Min($w, $dst.Width); $ht = [Math]::Min($ht, $dst.Height)
  $nx = [int]($dst.X + $relX * $dst.Width)
  $ny = [int]($dst.Y + $relY * $dst.Height)
  $nx = [Math]::Max($dst.X, [Math]::Min($nx, $dst.X + $dst.Width - $w))
  $ny = [Math]::Max($dst.Y, [Math]::Min($ny, $dst.Y + $dst.Height - $ht))
  [void][JvWin]::SetWindowPos($h, [IntPtr]::Zero, $nx, $ny, $w, $ht, 0x0004)
  if ($max) { [void][JvWin]::ShowWindow($h, 3) }
}
'''


class _PSWorker:
    """Processo PowerShell sempre acceso, per i comandi che devono volare.

    I comandi si mandano e basta (nessuna lettura dell'output): ai gesti serve
    velocita, non conferma. Se il processo muore viene ricreato al volo.
    """

    def __init__(self):
        self._proc = None
        self._lock = threading.Lock()

    def _spawn(self):
        proc = subprocess.Popen(
            ["powershell", "-NoProfile", "-NonInteractive",
             "-ExecutionPolicy", "Bypass", "-Command", "-"],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        proc.stdin.write(_PS_PRELUDE + "\n")
        proc.stdin.flush()
        return proc

    def send(self, command):
        with self._lock:
            try:
                if self._proc is None or self._proc.poll() is not None:
                    self._proc = self._spawn()
                self._proc.stdin.write(command + "\n")
                self._proc.stdin.flush()
                return True
            except (OSError, ValueError):
                self._proc = None
                return False

    def stop(self):
        with self._lock:
            if self._proc and self._proc.poll() is None:
                try:
                    self._proc.stdin.write("exit\n")
                    self._proc.stdin.flush()
                except (OSError, ValueError):
                    self._proc.kill()
            self._proc = None


_worker = _PSWorker()


def _ok(**kw):
    d = {"status": "ok"}
    d.update(kw)
    return d


def _err(message):
    return {"status": "error", "message": message}


def _sendkeys(keys_expr, repeat=1):
    """Invia tasti virtuali tramite WScript.Shell (volume, media, scorciatoie)."""
    ps = (
        "$w = New-Object -ComObject WScript.Shell;"
        f"1..{int(repeat)} | ForEach-Object {{ $w.SendKeys({keys_expr}); Start-Sleep -Milliseconds 12 }}"
    )
    return _run_ps(ps, timeout=45)


# ---- Esecuzione comandi ----------------------------------------------------

def run_powershell(command, confirm=False, timeout=30):
    """Esegue un comando PowerShell arbitrario (controllo completo del PC)."""
    if not command:
        return _err("Comando vuoto.")
    if _DESTRUCTIVE.search(command) and not confirm:
        return {
            "status": "needs_confirmation",
            "message": ("Comando potenzialmente distruttivo o irreversibile. "
                        "Spiega al signore cosa succedera e chiedi conferma esplicita, "
                        "poi richiama lo stesso comando con confirm=true."),
            "command": command,
        }
    r = _run_ps(command, timeout=min(int(timeout or 30), 120))
    r["status"] = "ok" if r["exit_code"] == 0 else "error"
    return r


# ---- App, file, cartelle ---------------------------------------------------

# NB: usare la sintassi %VAR% e non $env:VAR — questi valori finiscono dentro
# apici singoli di PowerShell, che sono letterali e non espandono nulla.
# L'espansione la facciamo in Python con os.path.expandvars().
_KNOWN_APPS = {
    "vs code": r"%LOCALAPPDATA%\Programs\Microsoft VS Code\Code.exe",
    "vscode": r"%LOCALAPPDATA%\Programs\Microsoft VS Code\Code.exe",
    "visual studio code": r"%LOCALAPPDATA%\Programs\Microsoft VS Code\Code.exe",
    "codice": r"%LOCALAPPDATA%\Programs\Microsoft VS Code\Code.exe",
    "firefox": r"C:\Program Files\Mozilla Firefox\firefox.exe",
    "chrome": r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    "google chrome": r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    "edge": r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    "blocco note": "notepad.exe",
    "notepad": "notepad.exe",
    "calcolatrice": "calc.exe",
    "esplora risorse": "explorer.exe",
    "terminale": "wt.exe",
    "powershell": "powershell.exe",
    "impostazioni": "ms-settings:",
    "task manager": "taskmgr.exe",
    "gestione attivita": "taskmgr.exe",
    "paint": "mspaint.exe",
    "spotify": "spotify:",
}


def open_app(name):
    """Apre un'applicazione per nome (es. chrome, firefox, notepad, vs code)."""
    if not name:
        return _err("Nome app mancante.")
    key = name.strip().lower()
    target = os.path.expandvars(_KNOWN_APPS.get(key, name))
    # Prova diretta, poi fallback sulla ricerca nel menu Start
    r = _run_ps(f"Start-Process {_q(target)}")
    if r["exit_code"] == 0:
        return _ok(message=f"Aperto: {name}")
    r2 = _run_ps(f"Start-Process {_q(key)}")
    if r2["exit_code"] == 0:
        return _ok(message=f"Aperto: {name}")
    return _err(r["stderr"] or f"Impossibile aprire {name}")


def open_path(path):
    """Apre un file o una cartella con l'applicazione predefinita."""
    if not path:
        return _err("Percorso mancante.")
    r = _run_ps(f"Invoke-Item -LiteralPath {_q(path)}")
    if r["exit_code"] == 0:
        return _ok(message=f"Aperto: {path}")
    r2 = _run_ps(f"Start-Process explorer.exe {_q(path)}")
    if r2["exit_code"] == 0:
        return _ok(message=f"Aperto: {path}")
    return _err(r["stderr"] or "Percorso non valido")


def open_url(url):
    """Apre un indirizzo web nel browser predefinito."""
    if not url:
        return _err("URL mancante.")
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", url):
        url = "https://" + url
    r = _run_ps(f"Start-Process {_q(url)}")
    if r["exit_code"] == 0:
        return _ok(message=f"Aperto nel browser: {url}")
    return _err(r["stderr"] or "Impossibile aprire l'URL")


def list_directory(path):
    """Elenca i file di una cartella."""
    if not path:
        return _err("Percorso mancante.")
    ps = (f"Get-ChildItem -LiteralPath {_q(path)} -Force | "
          "Select-Object Name,@{n='KB';e={[math]::Round($_.Length/1KB,0)}},LastWriteTime | "
          "Format-Table -AutoSize | Out-String -Width 200")
    r = _run_ps(ps)
    if r["exit_code"] == 0:
        return _ok(listing=r["stdout"] or "(cartella vuota)")
    return _err(r["stderr"])


def search_files(query, root=None, max_results=40):
    """Cerca file per nome nel PC (ricorsivo)."""
    if not query:
        return _err("Testo di ricerca mancante.")
    base = root or os.path.expanduser("~")
    ps = (f"Get-ChildItem -LiteralPath {_q(base)} -Recurse -File -Filter {_q('*' + query + '*')} "
          "-ErrorAction SilentlyContinue | Select-Object -First "
          f"{int(max_results)} -ExpandProperty FullName")
    r = _run_ps(ps, timeout=60)
    if r["exit_code"] != 0 and not r["stdout"]:
        return _err(r["stderr"] or "Ricerca fallita")
    found = [l for l in r["stdout"].splitlines() if l.strip()]
    return _ok(count=len(found), files=found, searched_in=base)


def read_file(path, max_chars=6000):
    """Legge il contenuto di un file di testo."""
    if not path:
        return _err("Percorso mancante.")
    try:
        with open(path, "rb") as f:
            raw = f.read(int(max_chars) * 4 + 4)
    except FileNotFoundError:
        return _err(f"File non trovato: {path}")
    except (IsADirectoryError, PermissionError) as e:
        return _err(str(e))
    except OSError as e:
        return _err(str(e))
    # Su Windows convivono file UTF-8 e file ANSI (cp1252): prova entrambi.
    for enc in ("utf-8-sig", "cp1252"):
        try:
            content = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        content = raw.decode("utf-8", errors="replace")
    truncated = len(content) > int(max_chars)
    return _ok(path=path, truncated=truncated, content=content[:int(max_chars)])


def write_file(path, content, append=False, confirm=False):
    """Scrive o accoda testo su un file. Sovrascrivere richiede conferma."""
    if not path:
        return _err("Percorso mancante.")
    exists = os.path.exists(path)
    if exists and not append and not confirm:
        return {
            "status": "needs_confirmation",
            "message": (f"Il file {path} esiste gia e verrebbe sovrascritto. "
                        "Chiedi conferma al signore, poi richiama con confirm=true."),
            "path": path,
        }
    try:
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "a" if append else "w", encoding="utf-8") as f:
            f.write(content or "")
    except Exception as e:
        return _err(str(e))
    return _ok(message=("Testo accodato a " if append else "File scritto: ") + path)


# ---- Stato del sistema -----------------------------------------------------

def system_status():
    """Report completo dello stato del sistema (CPU, RAM, dischi, uptime, rete, batteria)."""
    ps = (
        "$os=Get-CimInstance Win32_OperatingSystem;"
        "$cpu=(Get-CimInstance Win32_Processor|Measure-Object -Property LoadPercentage -Average).Average;"
        "$rt=[math]::Round($os.TotalVisibleMemorySize/1MB,1);"
        "$ru=[math]::Round(($os.TotalVisibleMemorySize-$os.FreePhysicalMemory)/1MB,1);"
        "$up=(Get-Date)-$os.LastBootUpTime;"
        "$lines=@();"
        "$lines+=\"CPU: $cpu% di carico\";"
        "$lines+=\"RAM: $ru di $rt GB usati\";"
        "$lines+=('Uptime: {0}g {1}h {2}m' -f $up.Days,$up.Hours,$up.Minutes);"
        "Get-CimInstance Win32_LogicalDisk -Filter 'DriveType=3'|%{"
        "$lines+=('Disco {0} {1} GB liberi su {2} GB' -f $_.DeviceID,"
        "[math]::Round($_.FreeSpace/1GB,0),[math]::Round($_.Size/1GB,0))};"
        "$b=Get-CimInstance Win32_Battery -ErrorAction SilentlyContinue;"
        "if($b){$lines+=('Batteria: {0}%' -f $b.EstimatedChargeRemaining)};"
        "$net=Get-NetConnectionProfile -ErrorAction SilentlyContinue|Select-Object -First 1;"
        "if($net){$lines+=('Rete: {0} ({1})' -f $net.Name,$net.IPv4Connectivity)};"
        "$lines -join \"`n\""
    )
    r = _run_ps(ps, timeout=25)
    if r["exit_code"] == 0:
        return _ok(report=r["stdout"])
    return _err(r["stderr"])


def list_processes(sort_by="memory", top=12):
    """Elenca i processi attivi piu pesanti (memory o cpu)."""
    prop = "CPU" if str(sort_by).lower() == "cpu" else "WorkingSet"
    ps = (f"Get-Process | Sort-Object {prop} -Descending | Select-Object -First {int(top)} "
          "Name,Id,@{n='RAM_MB';e={[math]::Round($_.WorkingSet/1MB,0)}},"
          "@{n='CPU_s';e={[math]::Round($_.CPU,1)}} | Format-Table -AutoSize | Out-String -Width 120")
    r = _run_ps(ps)
    if r["exit_code"] == 0:
        return _ok(processes=r["stdout"])
    return _err(r["stderr"])


def kill_process(name_or_pid, confirm=False):
    """Termina un processo per nome o PID. Richiede conferma esplicita."""
    if not name_or_pid:
        return _err("Nome o PID mancante.")
    if not confirm:
        return {
            "status": "needs_confirmation",
            "message": (f"Sto per terminare '{name_or_pid}'. Eventuali dati non salvati andranno persi. "
                        "Chiedi conferma al signore, poi richiama con confirm=true."),
            "target": str(name_or_pid),
        }
    target = str(name_or_pid)
    if target.isdigit():
        ps = f"Stop-Process -Id {int(target)} -Force"
    else:
        ps = f"Stop-Process -Name {_q(target.replace('.exe', ''))} -Force"
    r = _run_ps(ps)
    if r["exit_code"] == 0:
        return _ok(message=f"Processo terminato: {target}")
    return _err(r["stderr"] or "Processo non trovato")


def power_control(action, confirm=False):
    """Blocca, sospende, riavvia o spegne il PC. Tutto tranne 'lock' richiede conferma."""
    action = (action or "").strip().lower()
    actions = {
        "lock": "rundll32.exe user32.dll,LockWorkStation",
        "sleep": "Add-Type -AssemblyName System.Windows.Forms; "
                 "[System.Windows.Forms.Application]::SetSuspendState('Suspend',$false,$false)",
        "restart": "Restart-Computer -Force",
        "shutdown": "Stop-Computer -Force",
        "logoff": "shutdown /l",
    }
    if action not in actions:
        return _err(f"Azione non valida. Usa: {', '.join(actions)}")
    if action != "lock" and not confirm:
        return {
            "status": "needs_confirmation",
            "message": (f"Sto per eseguire '{action}' sul PC. Avvisa il signore di salvare il lavoro "
                        "e chiedi conferma, poi richiama con confirm=true."),
            "action": action,
        }
    r = _run_ps(actions[action], timeout=15)
    if r["exit_code"] == 0:
        return _ok(message=f"Eseguito: {action}")
    return _err(r["stderr"])


# ---- Audio e multimedia ----------------------------------------------------

def get_volume():
    """Volume corrente in percentuale, oppure None se non leggibile."""
    r = _run_ps(_PS_PRELUDE + "\n[JvAudio]::Get()", timeout=30)
    try:
        return int(r["stdout"].strip().splitlines()[-1])
    except (ValueError, IndexError):
        return None


def set_volume(level=None, delta=None, mute=None):
    """Regola il volume di sistema: level 0-100, delta +/-, oppure mute.

    Usa l'API audio di Windows (millisecondi) invece di simulare i tasti volume:
    quella strada richiedeva fino a 50 pressioni e un secondo e mezzo, troppo
    per il controllo a gesti.
    """
    if mute is not None:
        if _worker.send("[JvAudio]::ToggleMute()"):
            return _ok(message="Audio silenziato o riattivato.")
        return _err("Controllo audio non disponibile.")

    if delta is not None:
        # Legge il valore attuale e ci somma il delta: assoluto, non a passi.
        attuale = get_volume()
        if attuale is None:
            return _err("Volume attuale non leggibile.")
        nuovo = max(0, min(100, attuale + int(delta)))
        if _worker.send(f"[JvAudio]::Set({nuovo})"):
            return _ok(message=f"Volume al {nuovo} per cento.", level=nuovo)
        return _err("Controllo audio non disponibile.")

    if level is not None:
        lvl = max(0, min(100, int(level)))
        if _worker.send(f"[JvAudio]::Set({lvl})"):
            return _ok(message=f"Volume al {lvl} per cento.", level=lvl)
        return _err("Controllo audio non disponibile.")

    return _err("Specifica level, delta oppure mute.")


def window_to_monitor(direction):
    """Sposta la finestra attiva su un altro schermo.

    direction: 'left', 'right', oppure 'other' per l'altro schermo (con due
    monitor non serve indicare un verso: e' semplicemente quello opposto).
    """
    d = (direction or "").strip().lower()
    mappa = {"left": "left", "sinistra": "left",
             "right": "right", "destra": "right",
             "other": "other", "altro": "other", "toggle": "other"}
    verso = mappa.get(d)
    if not verso:
        return _err("Direzione non valida: usa left, right oppure other.")
    if not _worker.send(f"Move-JvWindow '{verso}'"):
        return _err("Spostamento non riuscito.")
    testo = {"left": "Finestra spostata a sinistra.",
             "right": "Finestra spostata a destra.",
             "other": "Finestra spostata sull'altro schermo."}[verso]
    return _ok(message=testo)


def media_playing():
    """True se una riproduzione e' davvero in corso, False altrimenti.

    Serve prima di "mettere in pausa": il tasto multimediale e' un interruttore,
    quindi premuto a vuoto FARÀ PARTIRE la musica invece di fermarla. Meglio
    chiedere prima a Windows chi sta suonando.
    """
    ps = '''
Add-Type -AssemblyName System.Runtime.WindowsRuntime -ErrorAction SilentlyContinue
$null = [Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager, Windows.Media.Control, ContentType=WindowsRuntime]
$m = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
  $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
  $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' } | Select-Object -First 1
$op = [Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager]::RequestAsync()
$mgr = $m.MakeGenericMethod([Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager]).Invoke($null, @($op)).Result
$s = $mgr.GetCurrentSession()
if ($s -and $s.GetPlaybackInfo().PlaybackStatus -eq 'Playing') { 'PLAYING' } else { 'NO' }
'''
    r = _run_ps(ps, timeout=25)
    return r["stdout"].strip().endswith("PLAYING")


def pause_if_playing():
    """Mette in pausa solo se qualcosa sta effettivamente suonando."""
    try:
        if not media_playing():
            return _ok(message="Nessuna riproduzione in corso.", paused=False)
    except Exception:
        return _ok(message="Stato riproduzione non verificabile.", paused=False)
    media_control("play_pause")
    return _ok(message="Riproduzione messa in pausa.", paused=True)


def media_control(action):
    """Controlla la riproduzione multimediale: play_pause, next, previous, stop.

    Usa l'iniezione di tastiera a livello di sistema, non SendKeys: quest'ultimo
    consegna alla finestra in primo piano, quindi il comando finiva nell'editor
    o nel browser invece che nel lettore musicale.
    """
    # Codici dei tasti multimediali di Windows.
    tasti = {
        "play_pause": 0xB3, "play": 0xB3, "pause": 0xB3,
        "next": 0xB0, "successiva": 0xB0,
        "previous": 0xB1, "prev": 0xB1, "precedente": 0xB1,
        "stop": 0xB2,
    }
    vk = tasti.get((action or "").strip().lower())
    if vk is None:
        return _err(f"Azione non valida. Usa: {', '.join(sorted(set(tasti)))}")
    if _worker.send(f"[JvKeys]::Tap({vk})"):
        return _ok(message=f"Comando multimediale inviato: {action}")
    return _err("Controllo multimediale non disponibile.")


# ---- Schermo, finestre, input ---------------------------------------------

# ---- Occhi e mano: cattura schermo e mouse -----------------------------------
#
# Perche' la consapevolezza DPI e' la prima cosa e non un dettaglio: se lo
# schermo e' scalato (125%, 150%...) un processo NON consapevole vede un
# desktop finto e rimpicciolito, mentre il puntatore si muove su quello vero.
# Cattura e click finirebbero in due sistemi di coordinate diversi e OGNI click
# cadrebbe fuori bersaglio, con l'aggravante di sbagliare sempre di piu' man
# mano che ci si allontana dall'angolo in alto a sinistra. Qui la dichiarano
# entrambi, quindi lavorano tutti e due in pixel fisici veri.
#
# Su questa macchina la scalatura e' al 100% e la differenza non si vedrebbe:
# la dichiarazione serve perche' basta collegare un portatile 4K per rompere
# tutto in modo silenzioso e difficilissimo da diagnosticare.

_PS_DPI = r'''
Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public class JvDpi {
  [DllImport("user32.dll")] public static extern bool SetProcessDpiAwarenessContext(IntPtr v);
  // -4 = PER_MONITOR_AWARE_V2: coordinate in pixel fisici su ogni monitor.
  public static void Aware() { try { SetProcessDpiAwarenessContext(new IntPtr(-4)); } catch {} }
}
"@ -ErrorAction SilentlyContinue
try { [JvDpi]::Aware() } catch {}
'''


def _dpi_python():
    """Rende consapevole del DPI il processo Python (quello che muove il mouse)."""
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        try:                       # Windows piu' vecchi
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            pass


_dpi_python()


def _cattura(max_width=1400, monitor="attivo"):
    """Cattura lo schermo e restituisce ANCHE la geometria.

    La geometria non e' un di piu': senza sapere dove comincia l'area
    catturata e di quanto l'immagine e' stata rimpicciolita, non si puo'
    ritradurre in un punto dello schermo cio' che il modello indica
    sull'immagine. Con piu' monitor l'origine puo' pure essere negativa.

    monitor="attivo" cattura SOLO lo schermo che contiene la finestra in primo
    piano, ed e' il valore giusto per il pilota automatico. Con due monitor
    affiancati l'intero desktop e' 3840x1080: rimpicciolito per il modello
    diventa 1400x394, dove le scritte dei pulsanti sono illeggibili e il
    pilota clicca a caso. Un monitor solo diventa 1400x788, cioe' leggibile.
    """
    os.makedirs(TMP, exist_ok=True)
    path = os.path.join(TMP, f"shot_{uuid.uuid4().hex[:8]}.png")
    if monitor == "tutti":
        scelta = "$b=[System.Windows.Forms.SystemInformation]::VirtualScreen\n"
    elif isinstance(monitor, int) or (isinstance(monitor, str) and monitor.isdigit()):
        scelta = (f"$i=[int]{int(monitor)}\n"
                  "$s=@([System.Windows.Forms.Screen]::AllScreens | Sort-Object {$_.Bounds.X})\n"
                  "if($i -ge $s.Count){$i=0}\n"
                  "$b=$s[$i].Bounds\n")
    else:
        # Lo schermo che contiene la finestra attiva; se non si capisce, il
        # primario. Mai l'intero desktop: perderemmo meta' della risoluzione.
        scelta = (
            "$h=[JvCat]::GetForegroundWindow()\n"
            "$b=$null\n"
            "if($h -ne [IntPtr]::Zero){\n"
            "  $r=New-Object JvCat+RECT\n"
            "  if([JvCat]::GetWindowRect($h,[ref]$r)){\n"
            "    $cx=[int](($r.Left+$r.Right)/2); $cy=[int](($r.Top+$r.Bottom)/2)\n"
            "    $p=New-Object System.Drawing.Point($cx,$cy)\n"
            "    $b=[System.Windows.Forms.Screen]::FromPoint($p).Bounds\n"
            "  }\n"
            "}\n"
            "if($null -eq $b){$b=[System.Windows.Forms.Screen]::PrimaryScreen.Bounds}\n"
        )
    ps = _PS_DPI + (
        "Add-Type -AssemblyName System.Windows.Forms,System.Drawing\n"
        "Add-Type -TypeDefinition @\"\n"
        "using System;\n"
        "using System.Runtime.InteropServices;\n"
        "public class JvCat {\n"
        "  [StructLayout(LayoutKind.Sequential)] public struct RECT { public int Left, Top, Right, Bottom; }\n"
        "  [DllImport(\"user32.dll\")] public static extern IntPtr GetForegroundWindow();\n"
        "  [DllImport(\"user32.dll\")] public static extern bool GetWindowRect(IntPtr h, out RECT r);\n"
        "}\n"
        "\"@ -ErrorAction SilentlyContinue\n"
        + scelta +
        "$src=New-Object System.Drawing.Bitmap($b.Width,$b.Height)\n"
        "$g=[System.Drawing.Graphics]::FromImage($src)\n"
        "$g.CopyFromScreen($b.Location,[System.Drawing.Point]::Empty,$b.Size)\n"
        f"$mw={int(max_width)}\n"
        "if($src.Width -gt $mw){\n"
        "  $nh=[int]($src.Height*$mw/$src.Width)\n"
        "  $dst=New-Object System.Drawing.Bitmap($mw,$nh)\n"
        "  $g2=[System.Drawing.Graphics]::FromImage($dst)\n"
        "  $g2.InterpolationMode='HighQualityBicubic'\n"
        "  $g2.DrawImage($src,0,0,$mw,$nh)\n"
        f"  $dst.Save({_q(path)},[System.Drawing.Imaging.ImageFormat]::Png)\n"
        "  $iw=$dst.Width; $ih=$dst.Height\n"
        "  $g2.Dispose();$dst.Dispose()\n"
        "} else {\n"
        f"  $src.Save({_q(path)},[System.Drawing.Imaging.ImageFormat]::Png)\n"
        "  $iw=$src.Width; $ih=$src.Height\n"
        "}\n"
        "$g.Dispose();$src.Dispose()\n"
        "'GEO {0} {1} {2} {3} {4} {5}' -f $b.X,$b.Y,$b.Width,$b.Height,$iw,$ih\n"
    )
    r = _run_ps(ps, timeout=40)
    if not os.path.exists(path):
        return _err(r["stderr"] or "Cattura schermo fallita")
    try:
        with open(path, "rb") as f:
            data = base64.b64encode(f.read()).decode("ascii")
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    geo = None
    for riga in r["stdout"].splitlines():
        if riga.startswith("GEO "):
            p = riga.split()
            if len(p) == 7:
                try:
                    geo = [int(x) for x in p[1:]]
                except ValueError:
                    geo = None
    if not geo:
        return _err("Geometria dello schermo non leggibile: senza, i click "
                    "finirebbero nel posto sbagliato.")
    left, top, larghezza, altezza, img_w, img_h = geo
    return {"status": "ok", "image_base64": data, "media_type": "image/png",
            "left": left, "top": top, "larghezza": larghezza, "altezza": altezza,
            "img_w": img_w, "img_h": img_h}


def screenshot(max_width=1400):
    """Cattura lo schermo. Ritorna un PNG in base64 che Jarvis puo GUARDARE."""
    r = _cattura(max_width)
    if r.get("status") != "ok":
        return r
    # Il brain trasforma questo dict in un blocco immagine per il modello: gli
    # si passa solo cio' che gli serve, la geometria e' affare del pilota.
    return {"status": "ok", "image_base64": r["image_base64"],
            "media_type": r["media_type"]}


def rect_finestra(frammento_titolo):
    """Rettangolo sullo schermo della prima finestra col titolo indicato.

    Serve al pilota automatico per sapere DOVE NON cliccare: se cliccasse
    dentro la finestra di Jarvis comanderebbe se stesso, e sarebbe un anello di
    retroazione che non si spezza da solo.
    """
    frammento = (frammento_titolo or "").strip()
    if not frammento:
        return _err("Titolo mancante.")
    ps = _PS_DPI + (
        "Add-Type -TypeDefinition @\"\n"
        "using System;\n"
        "using System.Runtime.InteropServices;\n"
        "public class JvRect {\n"
        "  [StructLayout(LayoutKind.Sequential)] public struct RECT { public int Left, Top, Right, Bottom; }\n"
        "  [DllImport(\"user32.dll\")] public static extern bool GetWindowRect(IntPtr h, out RECT r);\n"
        "}\n"
        "\"@ -ErrorAction SilentlyContinue\n"
        "$p = Get-Process | Where-Object { $_.MainWindowHandle -ne 0 -and "
        f"$_.MainWindowTitle -like {_q('*' + frammento + '*')} " "} | Select-Object -First 1\n"
        "if ($p) {\n"
        "  $r = New-Object JvRect+RECT\n"
        "  if ([JvRect]::GetWindowRect($p.MainWindowHandle, [ref]$r)) {\n"
        "    'RECT {0} {1} {2} {3}' -f $r.Left,$r.Top,$r.Right,$r.Bottom\n"
        "  }\n"
        "}\n"
    )
    r = _run_ps(ps, timeout=20)
    for riga in r["stdout"].splitlines():
        if riga.startswith("RECT "):
            p = riga.split()
            if len(p) == 5:
                try:
                    l, t, rr, b = (int(v) for v in p[1:])
                except ValueError:
                    break
                return _ok(left=l, top=t, right=rr, bottom=b)
    return _err("Finestra non trovata.")


def punto_schermo(x1000, y1000, geo):
    """Traduce un punto indicato dal modello (0-1000) in un pixel dello schermo.

    Il modello guarda un'immagine rimpicciolita e ragiona in coordinate
    normalizzate; il mouse vive nei pixel veri del desktop, che con piu'
    monitor puo' partire da un'origine negativa. Qui in mezzo c'e' l'unico
    calcolo che, se sbagliato, manda ogni singolo click fuori bersaglio: per
    questo e' una funzione pura, isolata e coperta da test.
    """
    fx = min(max(float(x1000), 0.0), 1000.0) / 1000.0
    fy = min(max(float(y1000), 0.0), 1000.0) / 1000.0
    x = int(round(geo["left"] + fx * geo["larghezza"]))
    y = int(round(geo["top"] + fy * geo["altezza"]))
    # Un punto sul bordo destro/basso cadrebbe appena FUORI dallo schermo.
    x = min(x, geo["left"] + geo["larghezza"] - 1)
    y = min(y, geo["top"] + geo["altezza"] - 1)
    return x, y


# ---- Mouse -------------------------------------------------------------------
#
# In Python con ctypes e non in PowerShell: qui non serve avviare un processo
# (0,35 s) e soprattutto non c'e' nessuna virgoletta da far sopravvivere a due
# livelli di escape, che in questo progetto ha gia' prodotto danni.
#
# SendInput e non mouse_event: quest'ultimo e' deprecato da vent'anni e
# Microsoft stessa lo sconsiglia. SetCursorPos per lo spostamento perche'
# accetta direttamente le coordinate del desktop virtuale, comprese quelle
# negative del monitor a sinistra del principale; le coordinate assolute di
# SendInput andrebbero invece normalizzate a 0-65535 sul solo monitor
# primario, ed e' una sorgente classica di click fuori bersaglio.

_INPUT_MOUSE = 0
_MOVE, _ABSOLUTE = 0x0001, 0x8000
_TASTI = {                       # (giu', su)
    "left": (0x0002, 0x0004),
    "right": (0x0008, 0x0010),
    "middle": (0x0020, 0x0040),
}
_WHEEL, _HWHEEL = 0x0800, 0x1000
_ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long),
                ("mouseData", ctypes.c_ulong), ("dwFlags", ctypes.c_ulong),
                ("time", ctypes.c_ulong), ("dwExtraInfo", ctypes.POINTER(_ULONG_PTR))]


class _INPUT(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("mi", _MOUSEINPUT)]
    _anonymous_ = ("u",)
    _fields_ = [("type", ctypes.c_ulong), ("u", _U)]


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


def _invia(flags, dati=0):
    ev = _INPUT(type=_INPUT_MOUSE,
                mi=_MOUSEINPUT(0, 0, dati, flags, 0, None))
    n = ctypes.windll.user32.SendInput(1, ctypes.byref(ev), ctypes.sizeof(ev))
    return n == 1


def posizione_mouse():
    p = _POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(p))
    return p.x, p.y


def _dentro_schermo(x, y):
    u = ctypes.windll.user32
    sx, sy = u.GetSystemMetrics(76), u.GetSystemMetrics(77)
    sw, sh = u.GetSystemMetrics(78), u.GetSystemMetrics(79)
    return sx <= x < sx + sw and sy <= y < sy + sh


def mouse_move(x, y, morbido=True):
    """Porta il puntatore in (x, y), in pixel dello schermo.

    Il movimento e' graduale e non un teletrasporto: i menu a tendina, le
    barre degli strumenti e i suggerimenti si aprono al PASSAGGIO del mouse, e
    con un salto istantaneo il programma non riceve mai gli eventi intermedi.
    Molte voci di menu sarebbero semplicemente irraggiungibili.
    """
    x, y = int(x), int(y)
    if not _dentro_schermo(x, y):
        return _err(f"Punto ({x}, {y}) fuori dallo schermo: non ci clicco.")
    if morbido:
        x0, y0 = posizione_mouse()
        passi = 12
        for i in range(1, passi):
            k = i / passi
            ctypes.windll.user32.SetCursorPos(int(round(x0 + (x - x0) * k)),
                                              int(round(y0 + (y - y0) * k)))
            time.sleep(0.008)
    ctypes.windll.user32.SetCursorPos(x, y)
    time.sleep(0.05)         # tempo perche' l'interfaccia registri il passaggio
    return _ok(x=x, y=y)


def mouse_click(x=None, y=None, tasto="left", doppio=False):
    """Clicca, eventualmente dopo essersi spostato in (x, y)."""
    tasto = (tasto or "left").lower()
    if tasto not in _TASTI:
        return _err(f"Tasto '{tasto}' sconosciuto.")
    if x is not None and y is not None:
        r = mouse_move(x, y)
        if r.get("status") != "ok":
            return r
    giu, su = _TASTI[tasto]
    volte = 2 if doppio else 1
    for i in range(volte):
        if not _invia(giu) or not _invia(su):
            return _err("Windows ha rifiutato l'invio del click.")
        if i == 0 and doppio:
            time.sleep(0.06)     # sotto la soglia di sistema del doppio click
    px, py = posizione_mouse()
    return _ok(message=f"Click {tasto}{' doppio' if doppio else ''} in ({px}, {py}).",
               x=px, y=py)


def mouse_scroll(passi=3, orizzontale=False):
    """Scorre la rotellina: passi positivi = su (o a destra)."""
    try:
        n = int(passi)
    except (TypeError, ValueError):
        return _err("Numero di passi non valido.")
    n = max(-30, min(30, n))
    for _ in range(abs(n)):
        # 120 = una tacca, l'unita' standard di Windows.
        if not _invia(_HWHEEL if orizzontale else _WHEEL,
                      120 if n > 0 else -120):
            return _err("Windows ha rifiutato lo scorrimento.")
        time.sleep(0.02)
    return _ok(message=f"Scorrimento di {n} tacche.")


# ---- Scorrimento a gesti: QUALE finestra scorre ----------------------------
#
# La rotellina di Windows, quella vera come quella simulata con SendInput, va
# alla finestra SOTTO IL PUNTATORE ("scorri le finestre inattive al passaggio
# del mouse", attiva di default), non a quella che stai guardando. Con i gesti
# pero' il mouse non lo tocchi: resta dove l'hai lasciato, di solito sulla HUD
# dove hai appena cliccato GESTI. Misurato su Chrome: il gesto veniva
# riconosciuto, l'evento partiva, Windows lo accettava... e scorreva la HUD,
# mentre la pagina restava ferma. Da qui il "non funziona per nulla".
#
# Regola: scorre la finestra IN PRIMO PIANO, quella che stai usando. Se in primo
# piano c'e' Jarvis stesso (HUD o console), scorre quella subito sotto, cioe'
# l'app che usavi prima di cliccare sulla HUD. All'inizio di ogni passata il
# puntatore viene portato su un punto VISIBILE di quella finestra, una volta
# sola: non lo si riporta indietro a ogni evento, cosi' se nel frattempo usi il
# mouse non ci si contende il puntatore.

_GA_ROOT = 2
_GWL_EXSTYLE = -20
_WS_EX_ESCLUSI = 0x00000080 | 0x00000020 | 0x08000000   # TOOLWINDOW | TRANSPARENT | NOACTIVATE
_DWMWA_CLOAKED = 14
# Finestre della shell: desktop, barre delle applicazioni, Start e riquadri.
_CLASSI_SHELL = {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd",
                 "Windows.UI.Core.CoreWindow", "XamlExplorerHostIslandWindow",
                 "NotifyIconOverflowWindow", "TopLevelWindowForOverflowXamlIsland"}
_SESSIONE_SCROLL_S = 0.8     # pausa oltre cui il prossimo evento apre una nuova passata
_scroll_lock = threading.Lock()
_scroll_sessione = {"hwnd": None, "fg": None, "t": 0.0}
_api_scroll_cache = None


def _api_scroll():
    """Prototipi ctypes delle API usate per scegliere la finestra da scorrere.

    Su una libreria user32 PROPRIA: gli argtypes fissati qui non devono cambiare
    il comportamento di ctypes.windll.user32, condivisa con il resto del modulo.
    Senza restype espliciti, a 64 bit ctypes troncherebbe gli HWND a 32 bit.
    """
    global _api_scroll_cache
    if _api_scroll_cache is not None:
        return _api_scroll_cache
    from ctypes import wintypes as wt
    u = ctypes.WinDLL("user32", use_last_error=True)

    def proto(nome, res, *args):
        f = getattr(u, nome)
        f.restype, f.argtypes = res, list(args)
        return f

    enum_proc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    api = {
        "wt": wt,
        "EnumProc": enum_proc,
        "GetForegroundWindow": proto("GetForegroundWindow", wt.HWND),
        "GetAncestor": proto("GetAncestor", wt.HWND, wt.HWND, wt.UINT),
        "WindowFromPoint": proto("WindowFromPoint", wt.HWND, wt.POINT),
        "GetCursorPos": proto("GetCursorPos", wt.BOOL, ctypes.POINTER(wt.POINT)),
        "SetCursorPos": proto("SetCursorPos", wt.BOOL, ctypes.c_int, ctypes.c_int),
        "IsWindow": proto("IsWindow", wt.BOOL, wt.HWND),
        "IsWindowVisible": proto("IsWindowVisible", wt.BOOL, wt.HWND),
        "IsIconic": proto("IsIconic", wt.BOOL, wt.HWND),
        "GetWindowTextW": proto("GetWindowTextW", ctypes.c_int, wt.HWND, wt.LPWSTR, ctypes.c_int),
        "GetClassNameW": proto("GetClassNameW", ctypes.c_int, wt.HWND, wt.LPWSTR, ctypes.c_int),
        "GetClientRect": proto("GetClientRect", wt.BOOL, wt.HWND, ctypes.POINTER(wt.RECT)),
        "ClientToScreen": proto("ClientToScreen", wt.BOOL, wt.HWND, ctypes.POINTER(wt.POINT)),
        "EnumWindows": proto("EnumWindows", wt.BOOL, enum_proc, wt.LPARAM),
        "GetWindowLong": proto("GetWindowLongPtrW" if hasattr(u, "GetWindowLongPtrW")
                               else "GetWindowLongW", ctypes.c_ssize_t, wt.HWND, ctypes.c_int),
    }
    try:
        dwm = ctypes.WinDLL("dwmapi")
        f = dwm.DwmGetWindowAttribute
        f.restype = ctypes.c_long
        f.argtypes = [wt.HWND, wt.DWORD, ctypes.c_void_p, wt.DWORD]
        api["DwmGetWindowAttribute"] = f
    except (OSError, AttributeError):
        api["DwmGetWindowAttribute"] = None
    _api_scroll_cache = api
    return api


def _testo_finestra(api, h, classe=False):
    buf = ctypes.create_unicode_buffer(256)
    (api["GetClassNameW"] if classe else api["GetWindowTextW"])(h, buf, 256)
    return buf.value


def _nascosta_da_dwm(api, h):
    """Finestre 'visibili' per Windows ma non a schermo: app UWP sospese,
    finestre di altri desktop virtuali."""
    f = api["DwmGetWindowAttribute"]
    if not f:
        return False
    v = ctypes.c_uint(0)
    return f(h, _DWMWA_CLOAKED, ctypes.byref(v), ctypes.sizeof(v)) == 0 and v.value != 0


def _scorribile(api, h):
    """True se h e' una finestra d'applicazione che ha senso scorrere."""
    if not h or not api["IsWindowVisible"](h) or api["IsIconic"](h):
        return False
    if _testo_finestra(api, h, classe=True) in _CLASSI_SHELL:
        return False
    # Jarvis stesso: la HUD (Chrome) e la console hanno entrambe questo titolo.
    if "J.A.R.V.I.S." in _testo_finestra(api, h):
        return False
    if api["GetWindowLong"](h, _GWL_EXSTYLE) & _WS_EX_ESCLUSI:
        return False
    if _nascosta_da_dwm(api, h):
        return False
    r = api["wt"].RECT()
    api["GetClientRect"](h, ctypes.byref(r))
    return r.right - r.left >= 100 and r.bottom - r.top >= 80


def _finestra_da_scorrere(api):
    """La finestra in primo piano; se e' Jarvis (o la shell), quella subito sotto."""
    fg = api["GetForegroundWindow"]()
    if fg:
        fg = api["GetAncestor"](fg, _GA_ROOT) or fg
    if _scorribile(api, fg):
        return fg
    ordine = []                     # EnumWindows le elenca dall'alto verso il basso

    def raccogli(h, _l):
        ordine.append(h)
        return True
    api["EnumWindows"](api["EnumProc"](raccogli), 0)
    inizio = ordine.index(fg) + 1 if fg in ordine else 0
    for h in ordine[inizio:]:
        if _scorribile(api, h):
            return h
    return None


def _radice_sotto(api, x, y):
    """La finestra di primo livello che riceverebbe la rotellina nel punto (x, y)."""
    h = api["WindowFromPoint"](api["wt"].POINT(x, y))
    return api["GetAncestor"](h, _GA_ROOT) if h else None


def _punto_visibile(api, h):
    """Un punto della finestra h non coperto da altre finestre, il piu' vicino
    possibile al centro (dove di solito c'e' il contenuto da scorrere).
    None se la finestra e' coperta del tutto."""
    wt = api["wt"]
    r = wt.RECT()
    api["GetClientRect"](h, ctypes.byref(r))
    o = wt.POINT(0, 0)
    api["ClientToScreen"](h, ctypes.byref(o))
    w, alt = r.right - r.left, r.bottom - r.top
    if w <= 0 or alt <= 0:
        return None
    passi = [i / 8 for i in range(1, 8)]          # griglia 7x7, centro compreso
    for fx, fy in sorted(((fx, fy) for fx in passi for fy in passi),
                         key=lambda p: (p[0] - 0.5) ** 2 + (p[1] - 0.5) ** 2):
        x, y = o.x + int(w * fx), o.y + int(alt * fy)
        if _radice_sotto(api, x, y) == h:
            return x, y
    return None


def scroll_wheel(amount, orizzontale=False):
    """Un SINGOLO evento di rotellina, di quantita arbitraria (120 = una tacca),
    destinato alla finestra che stai usando.

    Pensato per lo scorrimento continuo guidato dai gesti: a differenza di
    mouse_scroll non fa un ciclo con pause (che bloccherebbe il thread e
    scatterebbe a tacche), ma invia un solo delta proporzionale al movimento
    della mano. Le app accettano delta piu piccoli di una tacca, quindi lo
    scorrimento risulta fluido e fine. Convenzione: positivo = su.

    Usa SendInput (rotellina HARDWARE reale): e' l'unico input che ogni app
    accetta, Chromium compreso. La finestra la sceglie _finestra_da_scorrere
    (vedi il commento qui sopra), e all'inizio di ogni passata il puntatore
    viene portato su di lei, perche' e' li' che Windows consegna la rotellina.
    Ritorna anche quale finestra sta scorrendo (hwnd e titolo, per il riscontro
    sulla HUD) e se questo evento ha aperto una nuova passata.
    """
    try:
        a = int(amount)
    except (TypeError, ValueError):
        return _err("Quantita di scorrimento non valida.")
    if a == 0:
        return _ok(message="Nessuno scorrimento.")
    # Tetto per singolo evento: uno scatto anomalo dei landmark non deve
    # catapultare la pagina a fondo articolo.
    a = max(-2400, min(2400, a))

    api = _api_scroll()
    with _scroll_lock:
        ora = time.monotonic()
        fg = api["GetForegroundWindow"]()
        s = _scroll_sessione
        h = s["hwnd"]
        continua = (h and ora - s["t"] < _SESSIONE_SCROLL_S and s["fg"] == fg
                    and api["IsWindow"](h) and api["IsWindowVisible"](h)
                    and not api["IsIconic"](h))
        if not continua:
            h = _finestra_da_scorrere(api)
            s["hwnd"] = None
            if not h:
                return _err("Nessuna finestra da scorrere: apri l'app da leggere.")
            p = api["wt"].POINT()
            api["GetCursorPos"](ctypes.byref(p))
            if _radice_sotto(api, p.x, p.y) != h:
                punto = _punto_visibile(api, h)
                if not punto:
                    return _err("La finestra da scorrere e' coperta da altre finestre.")
                api["SetCursorPos"](*punto)
            s["hwnd"], s["fg"] = h, fg
        s["t"] = ora
        if not _invia(_HWHEEL if orizzontale else _WHEEL, a):
            return _err("Windows ha rifiutato lo scorrimento.")
    titolo = _testo_finestra(api, h)[:80] or _testo_finestra(api, h, classe=True)
    return _ok(message=f"Scorrimento {a}: {titolo}.", finestra=titolo, hwnd=h,
               nuova=not continua)


def mouse_drag(x1, y1, x2, y2, tasto="left"):
    """Trascina da un punto all'altro tenendo premuto."""
    tasto = (tasto or "left").lower()
    if tasto not in _TASTI:
        return _err(f"Tasto '{tasto}' sconosciuto.")
    r = mouse_move(x1, y1)
    if r.get("status") != "ok":
        return r
    giu, su = _TASTI[tasto]
    if not _invia(giu):
        return _err("Windows ha rifiutato la pressione del tasto.")
    try:
        # Il trascinamento DEVE essere graduale: molti programmi iniziano il
        # trascinamento solo dopo aver visto il puntatore muoversi da premuto.
        r = mouse_move(x2, y2)
    finally:
        _invia(su)               # il tasto va rilasciato anche in caso di guaio
    if r.get("status") != "ok":
        return r
    return _ok(message=f"Trascinato da ({x1}, {y1}) a ({x2}, {y2}).")


def _paste_script(text, restore=True):
    """PowerShell che incolla `text` nella finestra attiva tramite gli appunti.

    Molto piu affidabile di SendKeys carattere per carattere: SendKeys sbaglia
    accenti, simboli e a capo, e tratta +^%~(){}[] come modificatori.
    """
    lines = ["$w = New-Object -ComObject WScript.Shell"]
    if restore:
        lines.append("$vecchio = Get-Clipboard -Raw -ErrorAction SilentlyContinue")
    lines += [
        f"Set-Clipboard -Value {_q(text)}",
        "Start-Sleep -Milliseconds 150",
        "$w.SendKeys('^v')",
        "Start-Sleep -Milliseconds 300",
    ]
    if restore:
        # Ripristina gli appunti: non vogliamo rubare quello che c'era prima.
        lines.append("if ($vecchio) { Set-Clipboard -Value $vecchio }")
    return ";".join(lines)


def type_text(text, expect_window=None, method="paste"):
    """Digita del testo nella finestra attiva (automazione tastiera).

    Se `expect_window` e indicato, il testo viene scritto SOLO se il titolo della
    finestra in primo piano lo contiene: evita di scrivere per sbaglio dentro un
    documento o un editor che il signore aveva aperto.
    """
    if not text:
        return _err("Testo mancante.")
    if expect_window:
        titolo = foreground_window()
        if str(expect_window).lower() not in titolo.lower():
            return _err(f"Non scrivo: mi aspettavo la finestra '{expect_window}' ma "
                        f"davanti c'e '{titolo}'. Mettila a fuoco e riprova.")
    if str(method).lower() != "sendkeys":
        r = _run_ps(_paste_script(str(text)), timeout=40)
        if r["exit_code"] == 0:
            return _ok(message=f"Scritto: {str(text)[:60]}")
        # Se gli appunti non sono accessibili, ripiega su SendKeys.
    escaped = re.sub(r"([+^%~(){}\[\]])", r"{\1}", str(text))
    r = _run_ps("$w = New-Object -ComObject WScript.Shell;"
                f"$w.SendKeys({_q(escaped)})", timeout=30)
    if r["exit_code"] == 0:
        return _ok(message=f"Scritto: {str(text)[:60]}")
    return _err(r["stderr"])


# App di messaggistica: come aprirle e come cercare un contatto.
_CHAT_APPS = {
    "whatsapp": {"target": "whatsapp:", "search": "^f", "load": 4.0, "title": "whatsapp"},
    "telegram": {"target": "tg://", "search": "^f", "load": 3.5, "title": "telegram"},
    "slack": {"target": "slack:", "search": "^k", "load": 4.5, "title": "slack"},
    "discord": {"target": "discord:", "search": "^k", "load": 4.5, "title": "discord"},
}


def send_message(app, contact, message, send=False, confirm=False):
    """Apre una chat con un contatto e scrive il messaggio.

    Di default NON invia: lascia il testo pronto perche il signore lo rilegga.
    L'invio effettivo (send=true) richiede conferma esplicita.
    """
    app_key = (app or "whatsapp").strip().lower()
    cfg = _CHAT_APPS.get(app_key)
    if not cfg:
        return _err(f"App non supportata. Usa: {', '.join(_CHAT_APPS)}")
    if not contact:
        return _err("Manca il nome del contatto.")
    if not message:
        return _err("Manca il testo del messaggio.")
    if send and not confirm:
        return {
            "status": "needs_confirmation",
            "message": (f"Sto per INVIARE questo messaggio a {contact} su {app_key}: "
                        f"\"{message}\". Rileggilo al signore e chiedi conferma; "
                        "solo dopo un si chiaro richiama con send=true e confirm=true."),
            "contact": contact, "text": message,
        }

    # 1) Avvia l'app e ASPETTA che sia davvero in primo piano. Senza questa
    #    verifica i tasti finirebbero nella finestra sbagliata (editor, browser...).
    _run_ps(f"Start-Process {_q(cfg['target'])}", timeout=25)
    time.sleep(cfg["load"])
    ok, title = _focus_and_verify(cfg["title"])
    if not ok:
        return _err(
            f"Non sono riuscito a portare {app_key} in primo piano (davanti c'e "
            f"'{title}'). Non ho scritto nulla per non digitare nella finestra "
            "sbagliata. Apri l'app e riprova.")

    esc = str(message).replace("'", "''")
    steps = [
        "$w = New-Object -ComObject WScript.Shell",
        "$vecchio = Get-Clipboard -Raw -ErrorAction SilentlyContinue",
        # Apre la ricerca contatti e digita il nome
        f"$w.SendKeys({_q(cfg['search'])})",
        "Start-Sleep -Milliseconds 700",
        f"Set-Clipboard -Value {_q(contact)}",
        "Start-Sleep -Milliseconds 150",
        "$w.SendKeys('^v')",
        "Start-Sleep -Milliseconds 1500",
        # Seleziona il primo risultato: il fuoco passa alla casella del messaggio
        "$w.SendKeys('{ENTER}')",
        "Start-Sleep -Milliseconds 1500",
        # Scrive il messaggio
        f"Set-Clipboard -Value '{esc}'",
        "Start-Sleep -Milliseconds 150",
        "$w.SendKeys('^v')",
        "Start-Sleep -Milliseconds 400",
    ]
    if send:
        steps += ["$w.SendKeys('{ENTER}')", "Start-Sleep -Milliseconds 300"]
    steps.append("if ($vecchio) { Set-Clipboard -Value $vecchio }")

    r = _run_ps(";".join(steps), timeout=60)
    if r["exit_code"] != 0:
        return _err(r["stderr"] or "Automazione non riuscita")
    if send:
        return _ok(message=f"Messaggio inviato a {contact} su {app_key}.")
    return _ok(message=(f"Chat con {contact} aperta su {app_key} e messaggio scritto, "
                        "pronto da inviare."),
               note="Non inviato: il signore deve premere Invio, oppure richiamare "
                    "send_message con send=true dopo conferma.")


def press_keys(keys):
    """Invia una scorciatoia da tastiera in sintassi SendKeys (es. '^s', '%{F4}', '{ENTER}')."""
    if not keys:
        return _err("Combinazione mancante.")
    r = _run_ps("$w = New-Object -ComObject WScript.Shell;"
                f"$w.SendKeys({_q(keys)})", timeout=20)
    if r["exit_code"] == 0:
        return _ok(message=f"Inviato: {keys}")
    return _err(r["stderr"])


def window_control(action, title=None):
    """Gestisce le finestre: focus (con title), minimize_all, restore_all, list."""
    action = (action or "").strip().lower()
    if action == "focus":
        if not title:
            return _err("Serve il titolo (anche parziale) della finestra.")
        ps = (f"$w=New-Object -ComObject WScript.Shell; if($w.AppActivate({_q(title)}))"
              "{'ok'}else{throw 'finestra non trovata'}")
        r = _run_ps(ps)
        if r["exit_code"] == 0:
            return _ok(message=f"Finestra in primo piano: {title}")
        return _err(f"Nessuna finestra trovata con titolo '{title}'")
    if action in ("minimize_all", "minimize"):
        r = _run_ps("(New-Object -ComObject Shell.Application).MinimizeAll()")
        return _ok(message="Finestre minimizzate.") if r["exit_code"] == 0 else _err(r["stderr"])
    if action in ("restore_all", "restore"):
        r = _run_ps("(New-Object -ComObject Shell.Application).UndoMinimizeALL()")
        return _ok(message="Finestre ripristinate.") if r["exit_code"] == 0 else _err(r["stderr"])
    if action == "list":
        ps = ("Get-Process | Where-Object {$_.MainWindowTitle} | "
              "Select-Object -ExpandProperty MainWindowTitle")
        r = _run_ps(ps)
        wins = [l for l in r["stdout"].splitlines() if l.strip()]
        return _ok(windows=wins, count=len(wins))
    return _err("Azione non valida. Usa: focus, minimize_all, restore_all, list")


def clipboard(action, text=None):
    """Legge (read) o scrive (write) gli appunti di Windows."""
    action = (action or "").strip().lower()
    if action == "read":
        r = _run_ps("Get-Clipboard -Raw")
        if r["exit_code"] == 0:
            return _ok(content=r["stdout"] or "(appunti vuoti)")
        return _err(r["stderr"])
    if action == "write":
        if text is None:
            return _err("Testo mancante.")
        r = _run_ps(f"Set-Clipboard -Value {_q(text)}")
        if r["exit_code"] == 0:
            return _ok(message="Copiato negli appunti.")
        return _err(r["stderr"])
    return _err("Azione non valida. Usa: read oppure write")


def notify(title, message):
    """Mostra una notifica di Windows sul desktop."""
    if not message:
        return _err("Messaggio mancante.")
    ttl = (title or "J.A.R.V.I.S.").replace("'", "''")
    msg = str(message).replace("'", "''")
    script = (
        "Add-Type -AssemblyName System.Windows.Forms,System.Drawing\n"
        "$n = New-Object System.Windows.Forms.NotifyIcon\n"
        "$n.Icon = [System.Drawing.SystemIcons]::Information\n"
        "$n.BalloonTipTitle = '" + ttl + "'\n"
        "$n.BalloonTipText = '" + msg + "'\n"
        "$n.Visible = $true\n"
        "$n.ShowBalloonTip(8000)\n"
        "Start-Sleep -Seconds 9\n"
        "$n.Dispose()\n"
    )
    try:
        _run_ps_detached(script)
    except Exception as e:
        return _err(str(e))
    return _ok(message="Notifica mostrata.")


# ---- Promemoria pianificati (sopravvivono al riavvio) ----------------------

def schedule_reminder(text, when):
    """Programma un promemoria a un orario preciso. 'when' in formato ISO: 2026-07-21 15:30."""
    if not text:
        return _err("Testo del promemoria mancante.")
    if not when:
        return _err("Orario mancante (formato: 2026-07-21 15:30).")
    try:
        target = datetime.datetime.fromisoformat(str(when).strip().replace("T", " "))
    except ValueError:
        return _err("Formato orario non valido. Usa 'AAAA-MM-GG HH:MM'.")
    if target <= datetime.datetime.now():
        return _err("L'orario indicato e gia passato.")

    name = f"Jarvis_{uuid.uuid4().hex[:8]}"
    msg = str(text).replace("'", "''").replace('"', "'")
    porta = os.environ.get("JARVIS_PORT", "8730")
    # All'orario stabilito si prova prima a farlo dire a Jarvis con la sua voce.
    # Se la finestra e' chiusa (nessuno collegato) si ripiega sulla notifica di
    # Windows con la voce di sistema, cosi' il promemoria non va mai perso.
    inner = (
        "$detto=$false; "
        "try { "
        f"  $r = Invoke-RestMethod -Uri 'http://127.0.0.1:{porta}/api/announce' "
        "     -Method Post -ContentType 'application/json' -TimeoutSec 4 "
        f"     -Body (@{{text='Promemoria: {msg}'}} | ConvertTo-Json); "
        "  $detto = [bool]$r.delivered "
        "} catch { $detto = $false } "
        "if (-not $detto) { "
        "  Add-Type -AssemblyName System.Windows.Forms,System.Drawing; "
        "  $n=New-Object System.Windows.Forms.NotifyIcon; "
        "  $n.Icon=[System.Drawing.SystemIcons]::Information; "
        "  $n.BalloonTipTitle='Promemoria di Jarvis'; "
        f"  $n.BalloonTipText='{msg}'; "
        "  $n.Visible=$true; $n.ShowBalloonTip(15000); "
        f"  (New-Object -ComObject SAPI.SpVoice).Speak('{msg}'); "
        "  Start-Sleep -Seconds 16; $n.Dispose() "
        "}"
    )
    arg = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -Command \"" + inner + "\""
    ps = (
        f"$a = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument {_q(arg)};"
        f"$t = New-ScheduledTaskTrigger -Once -At (Get-Date {_q(target.strftime('%Y-%m-%dT%H:%M:%S'))});"
        f"$s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries;"
        f"Register-ScheduledTask -TaskName {_q(name)} -Action $a -Trigger $t -Settings $s -Force | Out-Null;"
        "'ok'"
    )
    r = _run_ps(ps, timeout=45)
    if r["exit_code"] != 0:
        return _err(r["stderr"] or "Impossibile creare il promemoria")
    _append_utf8(os.path.join(ROOT, "promemoria.md"),
                 f"- [{target:%Y-%m-%d %H:%M}] {text}  ({name})")
    return _ok(message=f"Promemoria impostato per il {target:%d/%m alle %H:%M}.", task=name)


def list_reminders():
    """Elenca i promemoria pianificati da Jarvis."""
    ps = ("Get-ScheduledTask -TaskName 'Jarvis_*' -ErrorAction SilentlyContinue | "
          "ForEach-Object { $i = $_ | Get-ScheduledTaskInfo; "
          "'{0} -> {1}' -f $_.TaskName, $i.NextRunTime }")
    r = _run_ps(ps, timeout=30)
    items = [l for l in r["stdout"].splitlines() if l.strip()]
    return _ok(count=len(items), reminders=items or ["Nessun promemoria attivo."])


def cancel_reminder(task_name):
    """Annulla un promemoria pianificato dato il suo nome (Jarvis_xxxxxxxx)."""
    if not task_name or not str(task_name).startswith("Jarvis_"):
        return _err("Nome non valido: deve iniziare con 'Jarvis_'.")
    r = _run_ps(f"Unregister-ScheduledTask -TaskName {_q(task_name)} -Confirm:$false")
    if r["exit_code"] == 0:
        return _ok(message=f"Promemoria annullato: {task_name}")
    return _err(r["stderr"] or "Promemoria non trovato")


# ---- Note, to-do, memoria persistente --------------------------------------

def _append_utf8(path, line):
    with open(path, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def add_todo(text):
    """Aggiunge una voce alla to-do list."""
    if not text:
        return _err("Testo mancante.")
    _append_utf8(os.path.join(ROOT, "todo.md"), f"- [ ] {text}")
    return _ok(message=f"Aggiunto ai todo: {text}")


def list_todos():
    """Mostra la to-do list."""
    path = os.path.join(ROOT, "todo.md")
    if not os.path.exists(path):
        return _ok(todos=[], message="Nessun todo.")
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        items = [l.strip() for l in f if l.strip().startswith("- [")]
    return _ok(count=len(items), todos=items[-40:])


def add_note(text):
    """Salva una nota rapida con data e ora."""
    if not text:
        return _err("Testo mancante.")
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    _append_utf8(os.path.join(ROOT, "note.md"), f"- [{stamp}] {text}")
    return _ok(message="Nota salvata.")


def _load_memory():
    if not os.path.exists(MEMORY_FILE):
        return []
    try:
        with open(MEMORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def remember(fact):
    """Memorizza in modo permanente un fatto sul signore o sulle sue preferenze."""
    if not fact:
        return _err("Fatto mancante.")
    facts = _load_memory()
    entry = {"fact": str(fact).strip(), "date": datetime.date.today().isoformat()}
    if any(f.get("fact", "").lower() == entry["fact"].lower() for f in facts):
        return _ok(message="Lo sapevo gia, non l'ho duplicato.")
    facts.append(entry)
    with open(MEMORY_FILE, "w", encoding="utf-8") as f:
        json.dump(facts[-200:], f, ensure_ascii=False, indent=2)
    return _ok(message="Memorizzato.")


def forget(fragment):
    """Cancella dalla memoria i fatti che contengono un certo testo."""
    if not fragment:
        return _err("Testo mancante.")
    facts = _load_memory()
    frag = str(fragment).lower()
    kept = [f for f in facts if frag not in f.get("fact", "").lower()]
    removed = len(facts) - len(kept)
    with open(MEMORY_FILE, "w", encoding="utf-8") as f:
        json.dump(kept, f, ensure_ascii=False, indent=2)
    return _ok(message=f"Dimenticati {removed} elementi.")


def memory_digest(limit=60):
    """Testo compatto della memoria, da iniettare nel prompt di sistema."""
    facts = _load_memory()[-int(limit):]
    if not facts:
        return ""
    return "\n".join(f"- {f['fact']}" for f in facts if f.get("fact"))


# ---- Usato dall'endpoint REST /api/stats per il pannello HUD ----------------

def system_status_brief():
    ps = (
        "$os=Get-CimInstance Win32_OperatingSystem;"
        "$cpu=(Get-CimInstance Win32_Processor|Measure-Object -Property LoadPercentage -Average).Average;"
        "$c=Get-CimInstance Win32_LogicalDisk -Filter \"DeviceID='C:'\";"
        "$b=Get-CimInstance Win32_Battery -ErrorAction SilentlyContinue;"
        "[pscustomobject]@{cpu=[int]$cpu;"
        "ramUsed=[math]::Round(($os.TotalVisibleMemorySize-$os.FreePhysicalMemory)/1MB,1);"
        "ramTotal=[math]::Round($os.TotalVisibleMemorySize/1MB,1);"
        "diskFree=[math]::Round($c.FreeSpace/1GB,0);"
        "diskTotal=[math]::Round($c.Size/1GB,0);"
        "battery=$(if($b){[int]$b.EstimatedChargeRemaining}else{-1})}|ConvertTo-Json -Compress"
    )
    r = _run_ps(ps, timeout=15)
    try:
        return json.loads(r["stdout"])
    except (json.JSONDecodeError, TypeError):
        return {"cpu": 0, "ramUsed": 0, "ramTotal": 0,
                "diskFree": 0, "diskTotal": 0, "battery": -1}
