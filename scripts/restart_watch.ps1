# Argus-Watch 재기동 — schtasks ArgusWatch (register_watch.ps1 등록 전제).
#   powershell -ExecutionPolicy Bypass -File scripts\restart_watch.ps1
param(
    [switch]$Quiet
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$task = "ArgusWatch"

$q = schtasks /Query /TN $task 2>$null
if (-not $q) {
    if (-not $Quiet) { Write-Host "[skip] $task 미등록 — scripts\register_watch.ps1 먼저" }
    exit 0
}

schtasks /End /TN $task | Out-Null
Start-Sleep -Seconds 3
schtasks /Run /TN $task | Out-Null
Start-Sleep -Seconds 2

if (-not $Quiet) {
    Write-Host "[OK] $task 재기동 요청 완료 — logs\watch.log 에 code_rev 확인"
}
