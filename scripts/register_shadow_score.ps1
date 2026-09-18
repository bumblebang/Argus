# 그림자 장부 일일 채점 스케줄 (관리자 권한 불필요).
#   평일: 18:30 (장 마감 후 history/snapshot 반영 여유)
#   실행:  powershell -ExecutionPolicy Bypass -File scripts\register_shadow_score.ps1
param(
    [string]$WeekdayAt = "18:30"
)

$bat = Join-Path $PSScriptRoot "run_shadow_score.bat"
$vbs = Join-Path $PSScriptRoot "run_hidden.vbs"
if (-not (Test-Path $bat)) { Write-Error "run_shadow_score.bat 없음: $bat"; exit 1 }
if (-not (Test-Path $vbs)) { Write-Error "run_hidden.vbs 없음: $vbs"; exit 1 }

$action = New-ScheduledTaskAction -Execute "C:\Windows\System32\wscript.exe" `
    -Argument "`"$vbs`" `"$bat`""
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun `
    -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 1) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

Unregister-ScheduledTask -TaskName "ArgusShadowScore" -Confirm:$false -ErrorAction SilentlyContinue

$days = "Monday", "Tuesday", "Wednesday", "Thursday", "Friday"
$tr = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $days -At $WeekdayAt
Register-ScheduledTask -TaskName "ArgusShadowScore" -Action $action -Trigger $tr `
    -Settings $settings `
    -Description "Argus shadow ledger daily score ($WeekdayAt)" -Force | Out-Null

Write-Host "[OK] ArgusShadowScore ($WeekdayAt weekdays)"
Write-Host "  해제: Unregister-ScheduledTask -TaskName ArgusShadowScore"
Write-Host "  로그: logs\shadow_score.run.log"
