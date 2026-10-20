$ErrorActionPreference = 'Stop'
$projectDir = Split-Path -Parent $PSScriptRoot
$workspace = Join-Path $projectDir 'var/wechatagent-runtime/workspace'
$clientDir = Join-Path $workspace 'state/hook/wechat-4.1.10.27'
$executable = Join-Path $clientDir 'Weixin.exe'
try {
    if (-not (Test-Path -LiteralPath $executable)) { throw 'Matching runtime is missing; see docs/WeChatAgent接入.md.' }
    $existing = @(Get-CimInstance Win32_Process -Filter "Name='Weixin.exe'" | Where-Object { $_.CommandLine -notmatch '--type=|--crashpad-handler' })
    if ($existing.Count) { throw 'Exit WeChat completely from its tray before opening the matching client.' }
    if ((Get-Item -LiteralPath $executable).VersionInfo.FileVersion -ne '4.1.10.27') { throw 'WeChat version mismatch.' }
    $signature = Get-AuthenticodeSignature -LiteralPath $executable
    if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notmatch 'Tencent') { throw 'WeChat publisher signature check failed.' }
    $build = Get-Content -LiteralPath (Join-Path $workspace 'state/hook/native/build.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    if ((Get-FileHash -LiteralPath (Join-Path $clientDir 'version.dll') -Algorithm SHA256).Hash.ToLowerInvariant() -ne $build.sha256) { throw 'Native sender hash mismatch.' }
    if (Get-NetTCPConnection -LocalPort 30001 -State Listen -ErrorAction SilentlyContinue) { throw 'Native port 30001 is already in use.' }
    # Visible because the user must perform WeChat authentication personally.
    Start-Process -FilePath $executable -ArgumentList @('StartPort=30001','CallBackURL="http://127.0.0.1:8767/unused"') -WorkingDirectory $clientDir -WindowStyle Normal | Out-Null
    Write-Host 'Matching WeChat opened. Finish login yourself; this does not send messages.'
} catch {
    Write-Host $_.Exception.Message -ForegroundColor Red
    exit 1
}
