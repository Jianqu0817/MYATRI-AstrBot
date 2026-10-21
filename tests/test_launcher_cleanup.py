"""Exercise the actual Windows PowerShell helpers without stopping real services."""
import os
from pathlib import Path
import subprocess

import pytest


pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows launcher")
ROOT = Path(__file__).resolve().parents[1]
POWERSHELL = str(Path(os.environ.get("SystemRoot", "C:/Windows")) /
                 "System32/WindowsPowerShell/v1.0/powershell.exe")


def run_ps(tmp_path, script):
    helper = str(ROOT / "scripts/wechat_services.ps1").replace("'", "''")
    path = tmp_path / "offline.ps1"
    path.write_text("$ErrorActionPreference='Stop'\n. '" + helper + "'\n"
                    "function Stop-MyatriProcessTree { throw 'Real process termination is forbidden in tests' }\n"
                    + script, encoding="utf-8-sig")
    result = subprocess.run([POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(path)],
                            capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr.decode(errors="replace")


def test_owned_services_and_launcher_are_selected_without_wechat_or_unrelated_python(tmp_path):
    run_ps(tmp_path, r'''
$root='D:\synthetic project\MYATRI-AstrBot'
$stamp=[datetime]'2026-10-21T10:00:00'
function P($n,$parent,$exe,$cmd) {
    [PSCustomObject]@{ProcessId=$n;ParentProcessId=$parent;ExecutablePath=$exe;CommandLine=$cmd;CreationDate=$stamp}
}
$rows=@(
 (P 10 0 "$root\.venv\Scripts\python.exe" 'python.exe -m myatri_bridge.server'),
 (P 11 10 'D:\conda\python.exe' 'python.exe -m myatri_bridge.server'),
 (P 20 0 "$root\upstream\AstrBot\.venv\Scripts\python.exe" 'python.exe main.py'),
 (P 21 20 'D:\conda\python.exe' 'python.exe main.py'),
 (P 30 0 "$root\var\wechatagent-runtime\runtimes\python\python.exe" ('python.exe -B "'+$root+'\var\wechatagent-runtime\workspace\scripts\reader-api.py"')),
 (P 40 0 'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe' ('powershell.exe -NoProfile -File "'+$root+'\scripts\start_wechat.ps1" -Live')),
 (P 50 0 'D:\other\.venv\Scripts\python.exe' 'python.exe main.py'),
 (P 51 0 'D:\other\python.exe' 'python.exe -m myatri_bridge.server'),
 (P 52 0 "$root\var\wechatagent-runtime\hook\Weixin.exe" 'Weixin.exe StartPort=30001'),
 (P 53 0 "$root\.venv\Scripts\python.exe" 'python.exe other.py -m myatri_bridge.server'),
 (P 54 0 "$root\.venv\Scripts\python.exe" 'python.exe monitor_context.py'),
 (P 55 0 "$root\var\wechatagent-runtime-other\python.exe" ('python.exe "'+$root+'\var\wechatagent-runtime\workspace\scripts\reader-api.py"'))
)
$plan=@(Get-MyatriServiceTargets $rows $root 999 $stamp.AddHours(1))
if (($plan.ProcessId | Sort-Object) -join ',' -ne '10,20,30,40') { throw ('Bad targets: '+($plan.ProcessId -join ',')) }
if ($plan[0].Role -ne 'launcher') { throw 'Old launcher must stop first' }
''')


def test_newer_process_ancestors_and_other_checkout_are_preserved(tmp_path):
    run_ps(tmp_path, r'''
$root='D:\project'
$old=[datetime]'2026-10-21T10:00:00'
$now=$old.AddHours(1)
$rows=@(
 [PSCustomObject]@{ProcessId=1;ParentProcessId=0;ExecutablePath='C:\Windows\powershell.exe';CommandLine='powershell.exe -File D:\project\scripts\start_wechat.ps1';CreationDate=$old},
 [PSCustomObject]@{ProcessId=2;ParentProcessId=1;ExecutablePath='C:\Windows\powershell.exe';CommandLine='current';CreationDate=$now},
 [PSCustomObject]@{ProcessId=3;ParentProcessId=0;ExecutablePath='D:\project\.venv\Scripts\python.exe';CommandLine='python.exe -m myatri_bridge.server';CreationDate=$now.AddMinutes(1)},
 [PSCustomObject]@{ProcessId=4;ParentProcessId=0;ExecutablePath='C:\Windows\powershell.exe';CommandLine='powershell.exe -File D:\project-other\scripts\start_wechat.ps1';CreationDate=$old}
)
if (@(Get-MyatriServiceTargets $rows $root 2 $now).Count -ne 0) { throw 'Unsafe process selected' }
''')


def test_reused_pid_aborts_before_any_stop(tmp_path):
    run_ps(tmp_path, r'''
$stamp=[datetime]'2026-10-21T10:00:00'
$original=[PSCustomObject]@{ProcessId=123;ParentProcessId=0;ExecutablePath='D:\project\.venv\Scripts\python.exe';CommandLine='python.exe -m myatri_bridge.server';CreationDate=$stamp}
function Get-CimInstance {
 param($ClassName,$Filter,$ErrorAction)
 if ($Filter) { return [PSCustomObject]@{ProcessId=123;CreationDate=$stamp.AddSeconds(1);ExecutablePath=$original.ExecutablePath;CommandLine=$original.CommandLine} }
 return $original
}
$rejected=$false
try { Stop-MyatriPreviousServices 'D:\project' $stamp.AddHours(1) } catch {
 if ($_.Exception.Message -notlike 'Process identity changed*') { throw }
 $rejected=$true
}
if (-not $rejected) { throw 'Reused PID was not rejected' }
''')


def test_unknown_occupied_port_is_not_stopped(tmp_path):
    run_ps(tmp_path, r'''
function Get-CimInstance { param($ClassName,$Filter,$ErrorAction) return @() }
function Get-NetTCPConnection { param($State,$ErrorAction) [PSCustomObject]@{LocalPort=5031;OwningProcess=456} }
$rejected=$false
try { Stop-MyatriPreviousServices 'D:\project' ([datetime]'2026-10-21') } catch {
 if ($_.Exception.Message -notlike 'Port 5031 belongs to an unverified service*') { throw }
 $rejected=$true
}
if (-not $rejected) { throw 'Unknown port owner was not rejected' }
''')


def test_verified_old_service_cleanup_executes_only_the_planned_pid(tmp_path):
    run_ps(tmp_path, r'''
$stamp=[datetime]'2026-10-21T10:00:00'
$original=[PSCustomObject]@{ProcessId=123;ParentProcessId=0;ExecutablePath='D:\project\.venv\Scripts\python.exe';CommandLine='python.exe -m myatri_bridge.server';CreationDate=$stamp}
$script:stopped=@()
function Get-CimInstance { param($ClassName,$Filter,$ErrorAction) return $original }
function Get-NetTCPConnection { param($State,$ErrorAction) return @() }
function Stop-MyatriProcessTree { param($ProcessId) $script:stopped += $ProcessId }
Stop-MyatriPreviousServices 'D:\project' $stamp.AddHours(1)
if ($script:stopped.Count -ne 1 -or $script:stopped[0] -ne 123) { throw 'Wrong process was stopped' }
''')


def test_launcher_scripts_parse_in_windows_powershell_and_cleanup_precedes_prepare(tmp_path):
    paths = ",".join("'" + str(ROOT / "scripts" / name).replace("'", "''") + "'"
                     for name in ("start_wechat.ps1", "wechat_services.ps1"))
    run_ps(tmp_path, "foreach ($file in @(" + paths + r''')) {
 $parseErrors=$null; $tokens=$null
 $null=[System.Management.Automation.Language.Parser]::ParseFile($file,[ref]$tokens,[ref]$parseErrors)
 if ($parseErrors) { throw ($parseErrors.Message -join '; ') }
}
''')
    source = (ROOT / "scripts/start_wechat.ps1").read_text(encoding="utf-8")
    assert source.index("Stop-MyatriPreviousServices -ProjectDir") < source.index("& uv sync")
    assert "WaitOne(8000)" in source
    for name in ("Start-WeChat.cmd", "Start-WeChat-Live.cmd"):
        assert "start_wechat.ps1" in (ROOT / name).read_text(encoding="utf-8")
