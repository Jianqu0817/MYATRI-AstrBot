$ErrorActionPreference = 'Stop'
$projectDir = Split-Path -Parent $PSScriptRoot
$workspace = Join-Path $projectDir 'var/wechatagent-runtime/workspace'
$table = Get-Content -LiteralPath (Join-Path $projectDir 'astrbot_plugin_myatri/wechat-versions.json') -Raw -Encoding UTF8 | ConvertFrom-Json
try {
    $build = Get-Content -LiteralPath (Join-Path $workspace 'state/hook/native/build.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    $spec = @($table.versions | Where-Object { $_.wechat_version -eq $build.targetVersion })
    if (-not $spec.Count) {
        $supported = ($table.versions | ForEach-Object { $_.wechat_version }) -join ', '
        throw "Hook build targets WeChat $($build.targetVersion), outside the compatibility table ($supported). Upgrade guide: docs/微信版本升级指南.md"
    }
    $clientDir = Join-Path $workspace $spec[0].client_dir
    $executable = Join-Path $clientDir 'Weixin.exe'
    if (-not (Test-Path -LiteralPath $executable)) { throw 'Matching runtime is missing; see docs/WeChatAgent接入.md.' }
    $existing = @(Get-CimInstance Win32_Process -Filter "Name='Weixin.exe'" | Where-Object { $_.CommandLine -notmatch '--type=|--crashpad-handler' })
    if ($existing.Count) { throw 'Exit WeChat completely from its tray before opening the matching client.' }
    if ((Get-Item -LiteralPath $executable).VersionInfo.FileVersion -ne $build.targetVersion) { throw "WeChat version mismatch: expected $($build.targetVersion)." }
    $signature = Get-AuthenticodeSignature -LiteralPath $executable
    if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notmatch 'Tencent') { throw 'WeChat publisher signature check failed.' }
    if ((Get-FileHash -LiteralPath (Join-Path $clientDir 'version.dll') -Algorithm SHA256).Hash.ToLowerInvariant() -ne $build.sha256) { throw 'Native sender hash mismatch.' }
    if (Get-NetTCPConnection -LocalPort 30001 -State Listen -ErrorAction SilentlyContinue) { throw 'Native port 30001 is already in use.' }
    # Visible because the user must perform WeChat authentication personally.
    Start-Process -FilePath $executable -ArgumentList @('StartPort=30001','CallBackURL="http://127.0.0.1:8767/unused"') -WorkingDirectory $clientDir -WindowStyle Normal | Out-Null
    Write-Host 'Matching WeChat opened. Finish login yourself; this does not send messages.'
} catch {
    Write-Host $_.Exception.Message -ForegroundColor Red
    exit 1
}
