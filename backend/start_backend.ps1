param(
  # Keep the development API port aligned with frontend/vite.config.ts.
  [int]$Port = 8001,
  [switch]$Reload
)

$ErrorActionPreference = 'Stop'
$backendRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = 'D:\uv\python\cpython-3.12-windows-x86_64-none\python.exe'
if (-not (Test-Path -LiteralPath $python)) { $python = (Get-Command python -ErrorAction SilentlyContinue).Source }
if (-not $python) { throw '未找到 Python 运行时。' }

Set-Location -LiteralPath $backendRoot
if ($Reload) {
  & $python '.\dev_start.py' '--backend-only' '--backend-port' ([string]$Port)
} else {
  & $python '.\dev_start.py' '--backend-only' '--backend-port' ([string]$Port) '--no-watch'
}
exit $LASTEXITCODE
