@echo off
title J.A.R.V.I.S.
cd /d "%~dp0voice"

if not exist "%~dp0.venv\Scripts\python.exe" (
    echo Primo avvio: preparo l'ambiente Python...
    python -m venv "%~dp0.venv" || goto :nopy
    "%~dp0.venv\Scripts\python.exe" -m pip install -q -r requirements.txt
)

if not exist "%~dp0voice\.env" (
    echo.
    echo  ATTENZIONE: manca il file voice\.env con la chiave API.
    echo  Copia voice\.env.example in voice\.env e inserisci GEMINI_API_KEY
    echo  ^(la chiave si crea su https://aistudio.google.com/apikey^),
    echo  altrimenti Jarvis parte in modalita locale limitata.
    echo.
)

"%~dp0.venv\Scripts\python.exe" app.py
goto :eof

:nopy
echo.
echo  Python non trovato. Installalo da https://python.org e riprova.
pause
