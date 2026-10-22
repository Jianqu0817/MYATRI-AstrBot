@echo off
setlocal
pushd "%~dp0"
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start_local_chat.ps1" %*
if errorlevel 1 pause
popd
