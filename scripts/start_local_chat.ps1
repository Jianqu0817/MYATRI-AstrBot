$ErrorActionPreference = 'Stop'
$projectDir = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectDir
$env:PYTHONUTF8 = '1'
try {
    & uv sync --frozen
    if ($LASTEXITCODE -ne 0) { throw 'Bridge environment sync failed.' }
    & .\.venv\Scripts\python.exe -m myatri_bridge.local_chat @args
    if ($LASTEXITCODE -ne 0) { throw 'Local chat stopped. Close the group service before switching modes.' }
} catch {
    Write-Error $_
    exit 1
}
