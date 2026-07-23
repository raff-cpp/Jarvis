' Jarvis - launcher della scorciatoia globale Ctrl+Alt+J.
'
' Lo lancia il collegamento sul desktop (creato da installa-scorciatoia.ps1)
' quando premi Ctrl+Alt+J. La scorciatoia sta a livello di Windows, non dentro
' il processo di Jarvis: per questo funziona ANCHE quando Jarvis e' spento.
'
'   - Jarvis gia' acceso  -> colpisce l'endpoint /wake: la HUD viene in primo
'                            piano e il microfono si apre gia' armato.
'   - Jarvis spento       -> avvia AVVIA-JARVIS.cmd.
'
' Gira sotto wscript: nessuna finestra lampeggia.
Option Explicit

Dim fso, shell, radice, porta, url, http, esito, richiamato

Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

' Questo script sta in scripts\, la radice del progetto e' la cartella sopra.
radice = fso.GetParentFolderName(fso.GetParentFolderName(WScript.ScriptFullName))

porta = LeggiPorta(radice & "\voice\.env")
url = "http://127.0.0.1:" & porta & "/wake"

' Se la POST va a buon fine, sulla porta di Jarvis risponde qualcuno: e' acceso.
' Se Jarvis e' spento la connessione viene RIFIUTATA subito e .send solleva un
' errore -> lo cattivo qui. Importante: leggo Err.Number in una variabile SUBITO
' dopo .send e NON tocco http.status quando la richiesta e' fallita (accedervi
' solleverebbe a sua volta un errore, e in VBScript "And" non e' a corto
' circuito: la vecchia condizione sbagliava proprio qui).
On Error Resume Next
Set http = CreateObject("MSXML2.ServerXMLHTTP.6.0")
' resolve, connect, send, receive (ms): tetti bassi, la porta e' locale.
http.setTimeouts 800, 800, 1000, 2000
http.open "POST", url, False
http.send ""
esito = Err.Number
On Error GoTo 0

richiamato = (esito = 0)

If Not richiamato Then
    ' Nessuno risponde sulla porta: Jarvis e' spento, lo avvio.
    ' Finestra normale (1): la console di avvio resta visibile come sempre.
    shell.Run """" & radice & "\AVVIA-JARVIS.cmd""", 1, False
End If

' Legge JARVIS_PORT dal .env; se manca usa il default 8730.
Function LeggiPorta(envPath)
    Dim f, linea, valore
    LeggiPorta = "8730"
    If Not fso.FileExists(envPath) Then Exit Function
    Set f = fso.OpenTextFile(envPath, 1)
    Do Until f.AtEndOfStream
        linea = Trim(f.ReadLine)
        If Left(linea, 1) <> "#" And InStr(linea, "JARVIS_PORT") = 1 Then
            valore = Trim(Mid(linea, InStr(linea, "=") + 1))
            If IsNumeric(valore) Then LeggiPorta = valore
        End If
    Loop
    f.Close
End Function
