"""Strumenti di Jarvis: controllo completo del PC Windows.

Ogni funzione ritorna un dict semplice (status + dati) che viene serializzato e
passato al modello come risultato del tool. La sicurezza e centralizzata qui: i
comandi potenzialmente distruttivi richiedono conferma esplicita dell'utente.

Nessuna chiave API viene mai toccata da questo modulo.
"""
import base64
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


def media_control(action):
    """Controlla la riproduzione multimediale: play_pause, next, previous, stop."""
    keys = {
        "play_pause": "[char]179", "play": "[char]179", "pause": "[char]179",
        "next": "[char]176", "previous": "[char]177", "prev": "[char]177",
        "stop": "[char]178",
    }
    key = keys.get((action or "").strip().lower())
    if not key:
        return _err(f"Azione non valida. Usa: {', '.join(sorted(set(keys)))}")
    r = _sendkeys(key)
    if r["exit_code"] == 0:
        return _ok(message=f"Comando multimediale inviato: {action}")
    return _err(r["stderr"])


# ---- Schermo, finestre, input ---------------------------------------------

def screenshot(max_width=1400):
    """Cattura lo schermo. Ritorna un PNG in base64 che Jarvis puo GUARDARE."""
    os.makedirs(TMP, exist_ok=True)
    path = os.path.join(TMP, f"shot_{uuid.uuid4().hex[:8]}.png")
    ps = (
        "Add-Type -AssemblyName System.Windows.Forms,System.Drawing;"
        "$b=[System.Windows.Forms.SystemInformation]::VirtualScreen;"
        "$src=New-Object System.Drawing.Bitmap($b.Width,$b.Height);"
        "$g=[System.Drawing.Graphics]::FromImage($src);"
        "$g.CopyFromScreen($b.Location,[System.Drawing.Point]::Empty,$b.Size);"
        f"$mw={int(max_width)};"
        "if($src.Width -gt $mw){"
        "$nh=[int]($src.Height*$mw/$src.Width);"
        "$dst=New-Object System.Drawing.Bitmap($mw,$nh);"
        "$g2=[System.Drawing.Graphics]::FromImage($dst);"
        "$g2.InterpolationMode='HighQualityBicubic';"
        "$g2.DrawImage($src,0,0,$mw,$nh);"
        f"$dst.Save({_q(path)},[System.Drawing.Imaging.ImageFormat]::Png);"
        "$g2.Dispose();$dst.Dispose()}"
        f"else{{$src.Save({_q(path)},[System.Drawing.Imaging.ImageFormat]::Png)}}"
        "$g.Dispose();$src.Dispose()"
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
    # Il brain trasforma questo dict in un blocco immagine per il modello.
    return {"status": "ok", "image_base64": data, "media_type": "image/png"}


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
    inner = (
        "Add-Type -AssemblyName System.Windows.Forms,System.Drawing; "
        "$n=New-Object System.Windows.Forms.NotifyIcon; "
        "$n.Icon=[System.Drawing.SystemIcons]::Information; "
        "$n.BalloonTipTitle='Promemoria di Jarvis'; "
        f"$n.BalloonTipText='{msg}'; "
        "$n.Visible=$true; $n.ShowBalloonTip(15000); Start-Sleep -Seconds 16; $n.Dispose(); "
        f"(New-Object -ComObject SAPI.SpVoice).Speak('{msg}')"
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
