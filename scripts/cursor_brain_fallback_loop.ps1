# Gated poller for Cursor brain fallback inbox.
# Wakes the agent at most once per unanswered request id.
# Inbox is <repo>/data/llm_inbox. Wake prompt uses repo-root paths;
# if Cursor workspace is the parent folder, also try argus/ prefix.
$ErrorActionPreference = "Continue"
$inbox = Join-Path (Split-Path $PSScriptRoot -Parent) "data\llm_inbox"
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
    Write-Output ('AGENT_LOOP_WAKE_cursor_brain_fallback {"prompt":"data/llm_inbox/request.json (워크스페이스가 상위면 argus/data/llm_inbox/request.json) 을 읽고, docs/cursor_brain_fallback.md 계약에 맞춰 같은 폴더에 response.json 작성. id는 request와 동일. schema(DecisionOutput|ValidationOutput)에 맞는 result JSON. BUY thesis와 market_view/reason은 [CURSOR_FALLBACK]로 시작. 요청 없으면 no-op.","reason":"new unanswered request ' + $id + '"}')
  } catch {
    continue
  }
}
