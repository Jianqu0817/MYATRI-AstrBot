# Process ownership checks shared by the launcher and offline tests.
function ConvertTo-MyatriPath([string]$Value) {
    if (-not $Value) { return '' }
    return [IO.Path]::GetFullPath($Value).Replace('/', '\').TrimEnd('\')
}

function Test-MyatriPythonEntry([string]$Command, [string]$Entry) {
    $tokens = @([regex]::Matches($Command.Replace('/', '\'), '"[^"\r\n]*"|[^\s]+') |
        ForEach-Object { $_.Value.Trim('"') })
    if ($tokens.Count -lt 2) { return $false }
    $index = 1
    while ($index -lt $tokens.Count -and $tokens[$index] -in @('-B','-u','-E','-s')) { $index++ }
    if ($index -ge $tokens.Count) { return $false }
    if ($Entry -eq 'myatri_bridge.server') {
        return $tokens[$index] -eq '-m' -and $index+1 -lt $tokens.Count -and $tokens[$index+1] -eq $Entry
    }
    return $tokens[$index] -eq $Entry
}

function Get-MyatriServiceTargets {
    param([object[]]$Processes, [string]$ProjectDir, [int]$CurrentProcessId,
          [datetime]$StartedBefore)
    $root = ConvertTo-MyatriPath $ProjectDir
    $readerRoot = $root + '\var\wechatagent-runtime\'
    $readerScript = $root + '\var\wechatagent-runtime\workspace\scripts\reader-api.py'
    $launcherScript = [regex]::Escape($root + '\scripts\start_wechat.ps1')
    $bridgePython = $root + '\.venv\Scripts\python.exe'
    $astrbotPython = $root + '\upstream\AstrBot\.venv\Scripts\python.exe'
    $byId = @{}
    foreach ($process in $Processes) { $byId[[int]$process.ProcessId] = $process }
    # Never kill an ancestor of this launcher (which would also kill us).
    $ancestors = @{}
    $cursor = $CurrentProcessId
    while ($byId.ContainsKey($cursor) -and -not $ancestors.ContainsKey($cursor)) {
        $ancestors[$cursor] = $true
        $cursor = [int]$byId[$cursor].ParentProcessId
    }
    $targets = @{}
    foreach ($process in $Processes) {
        if (-not $process.ExecutablePath -or -not $process.CommandLine -or -not $process.CreationDate) { continue }
        if ([datetime]$process.CreationDate -ge $StartedBefore) { continue }
        $exe = ConvertTo-MyatriPath $process.ExecutablePath
        $command = $process.CommandLine.Replace('/', '\')
        $parent = $byId[[int]$process.ParentProcessId]
        $parentExe = if ($parent) { ConvertTo-MyatriPath $parent.ExecutablePath } else { '' }
        $role = $null
        $target = $process
        if (($exe -match '\\(?:powershell|pwsh)\.exe$') -and
            $command -match ('(?i)(?:^|\s)-File\s+(?:"' + $launcherScript + '"|' + $launcherScript + ')(?:\s|$)')) {
            $role = 'launcher'
        } elseif ($exe.StartsWith($readerRoot, [StringComparison]::OrdinalIgnoreCase) -and
                  $exe -match '\\python(?:w)?\.exe$' -and
                  (Test-MyatriPythonEntry $command $readerScript)) {
            $role = 'reader'
        } elseif ((Test-MyatriPythonEntry $command 'myatri_bridge.server') -and
                  ($exe -eq $bridgePython -or $parentExe -eq $bridgePython)) {
            $role = 'bridge'
            if ($parentExe -eq $bridgePython -and (Test-MyatriPythonEntry $parent.CommandLine 'myatri_bridge.server') -and
                [datetime]$parent.CreationDate -le [datetime]$process.CreationDate) { $target = $parent }
        } elseif (((Test-MyatriPythonEntry $command 'main.py') -or
                   (Test-MyatriPythonEntry $command ($root + '\upstream\AstrBot\main.py'))) -and
                  ($exe -eq $astrbotPython -or $parentExe -eq $astrbotPython)) {
            $role = 'astrbot'
            if ($parentExe -eq $astrbotPython -and
                ((Test-MyatriPythonEntry $parent.CommandLine 'main.py') -or
                 (Test-MyatriPythonEntry $parent.CommandLine ($root + '\upstream\AstrBot\main.py'))) -and
                [datetime]$parent.CreationDate -le [datetime]$process.CreationDate) { $target = $parent }
        }
        if ($role -and -not $ancestors.ContainsKey([int]$target.ProcessId) -and
            [datetime]$target.CreationDate -lt $StartedBefore) {
            $targets[[int]$target.ProcessId] = [PSCustomObject]@{
                ProcessId = [int]$target.ProcessId
                CreationDate = [datetime]$target.CreationDate
                ExecutablePath = $target.ExecutablePath
                CommandLine = $target.CommandLine
                Role = $role
            }
        }
    }
    return @($targets.Values | Sort-Object @{Expression={if ($_.Role -eq 'launcher') { 0 } else { 1 }}}, ProcessId)
}

function Stop-MyatriProcessTree([int]$ProcessId) {
    & "$env:SystemRoot\System32\taskkill.exe" /PID $ProcessId /T /F | Out-Null
    if ($LASTEXITCODE -ne 0 -and (Get-CimInstance Win32_Process -Filter ('ProcessId=' + $ProcessId) -ErrorAction SilentlyContinue)) {
        throw ('Could not stop previous MYATRI process ' + $ProcessId + '.')
    }
}

function Stop-MyatriPreviousServices {
    param([string]$ProjectDir, [datetime]$StartedBefore)
    $snapshot = @(Get-CimInstance Win32_Process)
    $targets = @(Get-MyatriServiceTargets -Processes $snapshot -ProjectDir $ProjectDir -CurrentProcessId $PID -StartedBefore $StartedBefore)
    foreach ($target in $targets) {
        $current = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $target.ProcessId) -ErrorAction SilentlyContinue
        if (-not $current) { continue }
        # A PID can be reused after the snapshot; verify the complete identity.
        if ([datetime]$current.CreationDate -ne $target.CreationDate -or
            $current.ExecutablePath -ne $target.ExecutablePath -or $current.CommandLine -ne $target.CommandLine) {
            throw ('Process identity changed for PID ' + $target.ProcessId + '; no service was stopped for that PID.')
        }
        Write-Host ('Stopping previous MYATRI ' + $target.Role + ' (PID ' + $target.ProcessId + ').')
        Stop-MyatriProcessTree $target.ProcessId
    }
    $deadline = [DateTime]::UtcNow.AddSeconds(8)
    do {
        $busy = @(Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
            Where-Object { $_.LocalPort -in @(5031,6185,8766) })
        if (-not $busy) { return }
        # Only wait for identified old services. An unrelated listener is never killed.
        $unknown = @($busy | Where-Object {
            $listenerId = $_.OwningProcess
            -not ($snapshot | Where-Object { $_.ProcessId -eq $listenerId -and
                ($targets.ProcessId -contains $_.ProcessId -or $targets.ProcessId -contains $_.ParentProcessId) })
        })
        if ($unknown) { throw ('Port ' + $unknown[0].LocalPort + ' belongs to an unverified service; it was not stopped.') }
        Start-Sleep -Milliseconds 200
    } while ([DateTime]::UtcNow -lt $deadline)
    throw 'Previous MYATRI services did not release their ports within 8 seconds.'
}
