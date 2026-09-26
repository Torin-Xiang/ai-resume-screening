@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
".venv\Scripts\python.exe" -m resume_screening.cli.main serve --open
echo.
echo [stopped] Press any key to close this window.
pause >nul
