# Argus-Watch 상주 루프 + 워치독을 Windows 작업 스케줄러에 등록 (관리자 권한 불필요).
#   실행:  powershell -ExecutionPolicy Bypass -File scripts\register_watch.ps1
#   해제:  schtasks /Delete /TN ArgusWatch /F ; schtasks /Delete /TN ArgusWatchdog /F
#
# ArgusWatch      = 로그온 시 시작, 무한 실행, 크래시 시 자동 재시작(절전 차단은 watch.py 가 담당).
# ArgusWatchdog   = 5분마다 heartbeat 검사 -> 멈추면(hang) ArgusWatch 재기동.
# 무콘솔(pythonw)+scripts\watch.py 직접 실행 — argus.exe(콘솔 런처) 대신 stub 유지(동작 동일).
param(
    [int]$WatchdogEveryMin = 5
)

$root = Split-Path -Parent $PSScriptRoot
$pyw = Join-Path $root ".venv\Scripts\pythonw.exe"
$py  = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $pyw)) { Write-Error "venv pythonw 없음: $pyw"; exit 1 }
$me = "$env:USERDOMAIN\$env:USERNAME"

# ── ArgusWatch (상주 루프) ─────────────────────────────────
# 무콘솔(pythonw) 로 직접 실행 → 콘솔 Ctrl+C(0xC000013A) 면역. 로그는 logs\watch.log.
$watchAction  = New-ScheduledTaskAction -Execute $pyw -Argument "scripts\watch.py" -WorkingDirectory $root
$watchTrigger = New-ScheduledTaskTrigger -AtLogOn -User $me
# 무한 실행(ExecutionTimeLimit 0) + 크래시 시 1분 간격 3회 재시작 + 배터리에도 계속.
$watchSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName "ArgusWatch" -Action $watchAction -Trigger $watchTrigger `
    -Settings $watchSettings -User $me `
    -Description "Argus-Watch continuous monitor loop (paper/monitor)" -Force | Out-Null

# ── ArgusWatchdog (heartbeat 감시) ─────────────────────────
$wdAction  = New-ScheduledTaskAction -Execute $py -Argument "scripts\watchdog.py" -WorkingDirectory $root
$wdTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Minutes $WatchdogEveryMin) `
    -RepetitionDuration (New-TimeSpan -Days 3650)
$wdSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 5)
Register-ScheduledTask -TaskName "ArgusWatchdog" -Action $wdAction -Trigger $wdTrigger `
    -Settings $wdSettings -Description "Argus-Watch heartbeat watchdog (restart on hang)" -Force | Out-Null

Write-Host "[OK] ArgusWatch + ArgusWatchdog 등록 완료."
Write-Host "  git hook(머지 후 재기동):  powershell -ExecutionPolicy Bypass -File scripts\install_git_hooks.ps1"
Write-Host "  시작:  schtasks /Run /TN ArgusWatch"
Write-Host "  상태:  schtasks /Query /TN ArgusWatch ; schtasks /Query /TN ArgusWatchdog"
Write-Host "  로그:  logs\watch.run.log (프로세스), data\state\watch.heartbeat (생존)"
