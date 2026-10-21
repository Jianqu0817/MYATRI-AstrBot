@echo off
cd /d "%~dp0"
".venv\Scripts\python.exe" "scripts\monitor_context.py" --open-browser
if errorlevel 1 pause
