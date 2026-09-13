@echo off
setlocal
cd /d "%~dp0"
echo Installerer valgfri WebSocket-stotte i en egen .venv-mappe.
echo Ingen API-nokkel trengs, og ingen handel startes.
where py >nul 2>nul
if not errorlevel 1 (
    py -3 -m venv .venv
) else (
    python -m venv .venv
)
if errorlevel 1 goto fail
".venv\Scripts\python.exe" -m pip install -r requirements-websocket.txt
if errorlevel 1 goto fail
echo Ferdig. Bruk Start_NordBot.bat for a starte programmet i papirmodus.
pause
exit /b 0
:fail
echo Installasjonen feilet. NordBot kan fortsatt bruke REST uten tillegget.
pause
exit /b 1
