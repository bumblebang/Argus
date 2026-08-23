# 공개 페이지 발행 스케줄 등록 (관리자 권한 불필요, 현재 사용자 작업).
#   실행:  powershell -ExecutionPolicy Bypass -File scripts\register_public.ps1
#   해제:  schtasks /Delete /TN ArgusPublic /F
#
# publish_public.py → brief(TTL) + HTML + single_commit(amend+force-push).
# 기본: 매일 08:00 KST. -Times 로 조정.
param(
    [string[]]$Times = @("08:00"),
    [string]$TaskName = "ArgusPublic"
)

$bat = Join-Path $PSScriptRoot "run_public.bat"
$vbs = Join-Path $PSScriptRoot "run_hidden.vbs"
if (-not (Test-Path $bat)) { Write-Error "run_public.bat 없음: $bat"; exit 1 }
if (-not (Test-Path $vbs)) { Write-Error "run_hidden.vbs 없음: $vbs"; exit 1 }

$action = New-ScheduledTaskAction -Execute "C:\Windows\System32\wscript.exe" `
    -Argument "`"$vbs`" `"$bat`""
$triggers = $Times | ForEach-Object { New-ScheduledTaskTrigger -Daily -At $_ }
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun `
    -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 15)

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $triggers `
    -Settings $settings `
    -Description "Argus public share page — GitHub Pages publish (single_commit)" -Force | Out-Null

Write-Host "[OK] '$TaskName' 등록 완료 — 매일 $($Times -join ', ') 실행."
Write-Host "  상태:  schtasks /Query /TN $TaskName"
Write-Host "  지금:  schtasks /Run /TN $TaskName"
Write-Host "  해제:  schtasks /Delete /TN $TaskName /F"
Write-Host "  로그:  logs\public_page.run.log"
