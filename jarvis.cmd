@echo off
REM Shim per lanciare Jarvis da terminale: "jarvis <comando> [testo]"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\jarvis.ps1" %*
