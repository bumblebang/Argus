# Install post-merge hook into .git/hooks (local machine only, not committed).
#   powershell -ExecutionPolicy Bypass -File scripts\install_git_hooks.ps1
$root = Split-Path -Parent $PSScriptRoot
$dest = Join-Path $root ".git\hooks\post-merge"
$content = @'
#!/bin/sh
set -e
ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT"
PREV="${1:-}"
PY="$ROOT/.venv/Scripts/python.exe"
if [ ! -x "$PY" ]; then PY="$ROOT/.venv/bin/python"; fi
if [ ! -x "$PY" ]; then PY=python3; fi
exec "$PY" "$ROOT/scripts/post_merge_restart.py" "$PREV"
'@
Set-Content -Path $dest -Value $content -Encoding UTF8NoBOM
git config --unset core.hooksPath 2>$null
Write-Host "[OK] .git/hooks/post-merge (local only)"
