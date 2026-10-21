@echo off
setlocal
pushd "%~dp0"
".venv\Scripts\python.exe" -X utf8 scripts\harness_cli.py %*
if errorlevel 1 pause
popd
