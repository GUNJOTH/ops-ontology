param(
  [int]$BackendPort = 8001,
  [int]$FrontendPort = 5173
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = 'D:\uv\python\cpython-3.12-windows-x86_64-none\python.exe'
if (-not (Test-Path -LiteralPath $python)) { $python = (Get-Command python -ErrorAction SilentlyContinue).Source }
if (-not $python) { throw 'Python runtime was not found.' }
& $python (Join-Path $projectRoot 'backend\dev_start.py') --backend-port $BackendPort --frontend-port $FrontendPort
exit $LASTEXITCODE
