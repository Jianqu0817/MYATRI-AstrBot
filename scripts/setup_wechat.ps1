$ErrorActionPreference = 'Stop'
$projectDir = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectDir
$env:PYTHONUTF8 = '1'
try {
    $distribution = Get-Content -LiteralPath 'var/wechatagent-runtime/workspace/config/distribution.json' -Raw -Encoding UTF8 | ConvertFrom-Json
    & $distribution.python scripts/setup_wechat_reader.py
    if ($LASTEXITCODE -ne 0) { throw 'Reader setup did not complete. See the code above; retry after fixing it.' }
    Write-Host 'Reader configured. Next: double-click Start-WeChat.cmd.'
} catch {
    Write-Host $_.Exception.Message -ForegroundColor Red
    exit 1
}
