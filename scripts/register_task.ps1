# 장전 배치를 Windows 작업 스케줄러에 등록한다 (상주 데몬 아님).
#   실행:  powershell -ExecutionPolicy Bypass -File scripts\register_task.ps1
#   해제:  schtasks /Delete /TN ArgusBatch /F
#
# 기본: 매일 08:30·16:00 에 run_bot.bat (국면 + agent_cycle 페이퍼).
# 상주는 register_watch.ps1 (ArgusWatch) 이다.
param(
    [string[]]$Times = @("08:30", "16:00"),
    [string]$TaskName = "ArgusBatch"
)

$bat = Join-Path $PSScriptRoot "run_bot.bat"
if (-not (Test-Path $bat)) { Write-Error "run_bot.bat 없음: $bat"; exit 1 }

$action   = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"$bat`""
$triggers = $Times | ForEach-Object { New-ScheduledTaskTrigger -Daily -At $_ }
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun `
    -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 10)

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $triggers `
    -Settings $settings -Description "Argus pre/post-session batch (not the watch daemon)" -Force | Out-Null

Write-Host "[OK] '$TaskName' 등록 — 매일 $($Times -join ', ') 장전 배치."
Write-Host "  상태:  schtasks /Query /TN $TaskName"
Write-Host "  해제:  schtasks /Delete /TN $TaskName /F"
Write-Host "  로그:  logs\bot.run.log"
Write-Host "주의: 이 작업은 실주문을 내지 않는다. 상주는 ArgusWatch."
