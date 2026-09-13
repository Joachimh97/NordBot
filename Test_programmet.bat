@echo off
setlocal
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" run_tests.py
    pause
    exit /b
)
where py >nul 2>nul
if not errorlevel 1 (
    py -3 run_tests.py
    pause
    exit /b
)
python run_tests.py
pause
