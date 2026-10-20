$ErrorActionPreference = 'Stop'
$projectDir = Split-Path -Parent $PSScriptRoot
$env:PYTHONUTF8 = '1'
try {
    Set-Location -LiteralPath $projectDir
    & .\.venv\Scripts\python.exe scripts/prepare.py
    if ($LASTEXITCODE -ne 0) { throw 'Run the bridge launcher once to prepare its environment.' }
    Set-Location -LiteralPath (Join-Path $projectDir 'upstream/AstrBot')
    & uv sync --frozen --no-dev
    if ($LASTEXITCODE -ne 0) { throw 'AstrBot environment sync failed.' }
    & uv run --no-sync python main.py
    if ($LASTEXITCODE -ne 0) { throw 'AstrBot stopped with an error.' }
} catch {
    Write-Error $_
    exit 1
}
