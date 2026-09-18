# 밸류 스캔 스케줄 등록 (관리자 권한 불필요).
#   평일: 07:40 · 15:45 (회당 max_per_run, 기본 15 → 하루 ~30 LLM)
#   토요일: 10:00 1회 (weekend_max_per_run, 지도 재감정) — 일요일 없음
#   실행:  powershell -ExecutionPolicy Bypass -File scripts\register_value_scan.ps1
param(
    [string]$WeekdayAM = "07:40",
    [string]$WeekdayPM = "15:45",
    [string]$SaturdayAt = "10:00"
)

$bat = Join-Path $PSScriptRoot "run_value_scan.bat"
$vbs = Join-Path $PSScriptRoot "run_hidden.vbs"
if (-not (Test-Path $bat)) { Write-Error "run_value_scan.bat 없음: $bat"; exit 1 }
if (-not (Test-Path $vbs)) { Write-Error "run_hidden.vbs 없음: $vbs"; exit 1 }

$action = New-ScheduledTaskAction -Execute "C:\Windows\System32\wscript.exe" `
    -Argument "`"$vbs`" `"$bat`""
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun `
    -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 2) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

foreach ($old in @("ArgusValueScan", "ArgusValueScanWE")) {
    Unregister-ScheduledTask -TaskName $old -Confirm:$false -ErrorAction SilentlyContinue
}

$days = "Monday", "Tuesday", "Wednesday", "Thursday", "Friday"
$trAM = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $days -At $WeekdayAM
$trPM = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $days -At $WeekdayPM
Register-ScheduledTask -TaskName "ArgusValueScanAM" -Action $action -Trigger $trAM `
    -Settings $settings `
    -Description "Argus value scan — weekday pre-open ($WeekdayAM)" -Force | Out-Null
Register-ScheduledTask -TaskName "ArgusValueScanPM" -Action $action -Trigger $trPM `
    -Settings $settings `
    -Description "Argus value scan — weekday post-close ($WeekdayPM)" -Force | Out-Null

$trSat = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Saturday -At $SaturdayAt
Register-ScheduledTask -TaskName "ArgusValueScanSat" -Action $action -Trigger $trSat `
    -Settings $settings `
    -Description "Argus value scan — Saturday map refresh ($SaturdayAt)" -Force | Out-Null

Write-Host "[OK] ArgusValueScanAM ($WeekdayAM) · PM ($WeekdayPM) · Sat ($SaturdayAt)"
Write-Host "  해제: ArgusValueScanAM / PM / Sat"
Write-Host "  로그: logs\value_scan.run.log"
