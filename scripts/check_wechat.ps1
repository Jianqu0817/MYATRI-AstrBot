$ErrorActionPreference = 'Stop'
$projectDir = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectDir
$env:PYTHONUTF8 = '1'
& uv sync --frozen
if ($LASTEXITCODE -ne 0) { exit 1 }
& .\.venv\Scripts\python.exe scripts/check_wechatagent.py
exit $LASTEXITCODE
