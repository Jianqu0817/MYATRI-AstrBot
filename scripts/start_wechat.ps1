param([switch]$Live)
$ErrorActionPreference = 'Stop'
$projectDir = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectDir
$env:PYTHONUTF8 = '1'
$bridgeProcess = $null
$readerProcess = $null
$launcherOwned = $false
. (Join-Path $PSScriptRoot 'wechat_services.ps1')
$launcherStarted = (Get-Process -Id $PID).StartTime
$launcherMutex = New-Object System.Threading.Mutex($false, ('Local\MYATRI-WeChat-' + ($projectDir -replace '[^A-Za-z0-9]', '_')))
try {
    try { $launcherOwned = $launcherMutex.WaitOne(0) } catch [System.Threading.AbandonedMutexException] { $launcherOwned = $true }
    Stop-MyatriPreviousServices -ProjectDir $projectDir -StartedBefore $launcherStarted
    if (-not $launcherOwned) {
        try { $launcherOwned = $launcherMutex.WaitOne(8000) } catch [System.Threading.AbandonedMutexException] { $launcherOwned = $true }
    }
    if (-not $launcherOwned) { throw 'The previous launcher could not be replaced safely. Close its original window and retry.' }
    & uv sync --frozen
    if ($LASTEXITCODE -ne 0) { throw 'Bridge environment sync failed.' }
    & .\.venv\Scripts\python.exe scripts/prepare.py
    if ($LASTEXITCODE -ne 0) { throw 'Preparation failed.' }
    $workspace = Join-Path $projectDir 'var/wechatagent-runtime/workspace'
    $wechatSettings = & .\.venv\Scripts\python.exe -c "from astrbot_plugin_myatri.wechatagent_client import Settings; from urllib.parse import urlsplit; s=Settings.load('config/wechatagent.local.toml'); print(urlsplit(s.native_url).port or 80)"
    if ($LASTEXITCODE -ne 0) { throw 'WeChat settings could not be loaded. See docs/WeChatAgent接入.md.' }
    $nativePort = [int]$wechatSettings
    if (-not (Get-NetTCPConnection -State Listen -LocalPort $nativePort -ErrorAction SilentlyContinue)) {
        throw ('WeChat native backend (127.0.0.1:'+$nativePort+') is not running. Exit WeChat from its tray, open Open-WeChat.cmd, finish login, then retry this launcher.')
    }
    if (-not (Test-Path -LiteralPath (Join-Path $workspace 'state/weflow/WeFlow-config.json'))) { throw 'Run Setup-WeChat.cmd once to bind the reader to your account.' }
    $distribution = Get-Content -LiteralPath (Join-Path $workspace 'config/distribution.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    $readerScript = Join-Path $workspace 'scripts/reader-api.py'
    $readerProcess = Start-Process -FilePath $distribution.python -ArgumentList @('-B',('"'+$readerScript+'"')) -WorkingDirectory $workspace -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $projectDir 'var/reader.stdout.log') -RedirectStandardError (Join-Path $projectDir 'var/reader.stderr.log')
    $deadline = [DateTime]::UtcNow.AddSeconds(30)
    $readerReady = $false
    $io = Get-Content -LiteralPath (Join-Path $workspace 'state/wechat-io-config.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    while ([DateTime]::UtcNow -lt $deadline -and -not $readerProcess.HasExited) {
        try {
            $readerStatus = Invoke-RestMethod -Uri ($io.reader_base_url+'/api/v1/reader/status') -Headers @{Authorization=('Bearer '+$io.reader_token)} -TimeoutSec 1
            if ($readerStatus.success -and $readerStatus.backend -eq 'weflow-cli-nt') { $readerReady = $true; break }
        } catch { Start-Sleep -Milliseconds 250 }
    }
    $io = $null
    if (-not $readerReady) { throw 'Reader did not start. See var/reader.stderr.log.' }
    & .\.venv\Scripts\python.exe scripts/check_wechatagent.py --configure
    if ($LASTEXITCODE -ne 0) { throw 'WeChat backend is not ready. No service started.' }
    if ($Live) {
        & .\.venv\Scripts\python.exe scripts/set_bridge_live.py
        if ($LASTEXITCODE -ne 0) { throw 'Live configuration could not be saved.' }
    }
    Push-Location (Join-Path $projectDir 'upstream/AstrBot')
    try {
        & uv sync --frozen --no-dev
        if ($LASTEXITCODE -ne 0) { throw 'AstrBot environment sync failed.' }
    } finally { Pop-Location }
    $bridgeProcess = Start-Process -FilePath (Join-Path $projectDir '.venv/Scripts/python.exe') -ArgumentList @('-m','myatri_bridge.server') -WorkingDirectory $projectDir -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $projectDir 'var/bridge.stdout.log') -RedirectStandardError (Join-Path $projectDir 'var/bridge.stderr.log')
    $bridgeToken = [IO.File]::ReadAllText((Join-Path $projectDir 'var/bridge-token.txt')).Trim()
    $ready = $false
    $deadline = [DateTime]::UtcNow.AddSeconds(15)
    while ([DateTime]::UtcNow -lt $deadline -and -not $bridgeProcess.HasExited) {
        try {
            $result = Invoke-RestMethod -Uri 'http://127.0.0.1:8766/health' -Headers @{Authorization=('Bearer '+$bridgeToken)} -TimeoutSec 1
            if ($result.ok) { $ready = $true; break }
        } catch { Start-Sleep -Milliseconds 200 }
    }
    $bridgeToken = $null
    if (-not $ready) { throw 'Bridge did not start; inspect var/bridge.stderr.log.' }
    Write-Host 'Starting AstrBot with the checked WeChatAgent platform.'
    Write-Host ('Backend mode: '+$result.mode+'. live uses the existing model credential; offline uses fixtures.')
    Write-Host 'Press Ctrl+C to stop this launcher and its bridge child.'
    Set-Location -LiteralPath (Join-Path $projectDir 'upstream/AstrBot')
    & .\.venv\Scripts\python.exe main.py
    if ($LASTEXITCODE -ne 0) { throw 'AstrBot stopped with an error.' }
} catch {
    Write-Host $_.Exception.Message -ForegroundColor Red
    exit 1
} finally {
    if ($bridgeProcess -and -not $bridgeProcess.HasExited) {
        # Only this launcher's original child handle can reach this branch.
        & "$env:SystemRoot\System32\taskkill.exe" /PID $bridgeProcess.Id /T /F | Out-Null
    }
    if ($readerProcess -and -not $readerProcess.HasExited) {
        & "$env:SystemRoot\System32\taskkill.exe" /PID $readerProcess.Id /T /F | Out-Null
    }
    Set-Location -LiteralPath $projectDir
    if ($launcherOwned) { $launcherMutex.ReleaseMutex() }
    $launcherMutex.Dispose()
}
