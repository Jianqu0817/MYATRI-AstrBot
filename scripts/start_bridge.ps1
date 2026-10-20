$ErrorActionPreference = 'Stop'
$projectDir = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectDir
$env:PYTHONUTF8 = '1'
try {
    & uv sync --frozen
    if ($LASTEXITCODE -ne 0) { throw 'Bridge environment sync failed.' }
    & .\.venv\Scripts\python.exe scripts/prepare.py
    if ($LASTEXITCODE -ne 0) { throw 'Bridge preparation failed.' }
    & .\.venv\Scripts\python.exe -m myatri_bridge.server
    if ($LASTEXITCODE -ne 0) { throw 'Bridge server stopped with an error.' }
} catch {
    Write-Error $_
    exit 1
}
