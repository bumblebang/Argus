# Gated poller for Cursor brain fallback inbox.
# Wakes the agent at most once per unanswered request id.
# Inbox is <repo>/data/inbox (legacy data/llm_inbox may be a junction to here).
# Wake prompt uses repo-root paths; if Cursor workspace is the parent folder,
# also try argus/ prefix.
$ErrorActionPreference = "Continue"
$repoRoot = Split-Path $PSScriptRoot -Parent
$inboxNew = Join-Path $repoRoot "data\inbox"
$inboxLegacy = Join-Path $repoRoot "data\llm_inbox"
# Prefer real inbox; fall back to legacy junction/path if new missing.
if (Test-Path -LiteralPath $inboxNew) {
  $inbox = $inboxNew
} elseif (Test-Path -LiteralPath $inboxLegacy) {
  $inbox = $inboxLegacy
} else {
  $inbox = $inboxNew
}
$reqPath = Join-Path $inbox "request.json"
$respPath = Join-Path $inbox "response.json"
$lastWokeId = $null

while ($true) {
  Start-Sleep -Seconds 60
  if (-not (Test-Path -LiteralPath $reqPath)) {
    $lastWokeId = $null
    continue
  }
  try {
    $req = Get-Content -LiteralPath $reqPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $id = [string]$req.id
    if ([string]::IsNullOrWhiteSpace($id)) { continue }
    if ($id -eq $lastWokeId) { continue }
    if (Test-Path -LiteralPath $respPath) {
      $resp = Get-Content -LiteralPath $respPath -Raw -Encoding UTF8 | ConvertFrom-Json
      if ([string]$resp.id -eq $id) { continue }
    }
    $lastWokeId = $id
    Write-Output ('AGENT_LOOP_WAKE_cursor_brain_fallback {"prompt":"data/inbox/request.json (워크스페이스가 상위면 argus/data/inbox/request.json; 레거시 junction이면 data/llm_inbox 동일) 을 읽고, docs/cursor_brain_fallback.md 계약에 맞춰 같은 폴더에 response.json 작성. id는 request와 동일. schema(DecisionOutput|ValidationOutput)에 맞는 result JSON. BUY thesis와 market_view/reason은 [CURSOR_FALLBACK]로 시작. 요청 없으면 no-op.","reason":"new unanswered request ' + $id + '"}')
  } catch {
    continue
  }
}
