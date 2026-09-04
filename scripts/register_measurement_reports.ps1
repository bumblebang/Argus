# Tier0 측정 리포트 스케줄 (관리자 권한 불필요).
#   기본: 평일 19:00 (그림자 채점 18:30 이후 · 장후)
#   실행:  powershell -ExecutionPolicy Bypass -File scripts\register_measurement_reports.ps1
#   해제:  Unregister-ScheduledTask -TaskName ArgusMeasurementReports
param(
    [string]$WeekdayAt = "19:00"
)

$bat = Join-Path $PSScriptRoot "run_measurement_reports.bat"
$vbs = Join-Path $PSScriptRoot "run_hidden.vbs"
if (-not (Test-Path $bat)) { Write-Error "run_measurement_reports.bat 없음: $bat"; exit 1 }
if (-not (Test-Path $vbs)) { Write-Error "run_hidden.vbs 없음: $vbs"; exit 1 }

$action = New-ScheduledTaskAction -Execute "C:\Windows\System32\wscript.exe" `
    -Argument "`"$vbs`" `"$bat`""
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun `
    -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 1) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

Unregister-ScheduledTask -TaskName "ArgusMeasurementReports" -Confirm:$false -ErrorAction SilentlyContinue

$days = "Monday", "Tuesday", "Wednesday", "Thursday", "Friday"
$tr = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $days -At $WeekdayAt
Register-ScheduledTask -TaskName "ArgusMeasurementReports" -Action $action -Trigger $tr `
    -Settings $settings `
    -Description "Argus Tier0 measurement reports (dossier/wiring/baseline) at $WeekdayAt" -Force | Out-Null

Write-Host "[OK] ArgusMeasurementReports ($WeekdayAt weekdays)"
Write-Host "  해제: Unregister-ScheduledTask -TaskName ArgusMeasurementReports"
Write-Host "  로그: logs\measurement_reports.run.log"
Write-Host "  산출: data\dossier_quality.json / wiring_mismatch.json / measurement_baseline_latest.json"
