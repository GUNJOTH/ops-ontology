param(
  [int]$BackendPort = 8000,
  [int]$FrontendPort = 5173
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$backendRoot = Join-Path $projectRoot 'backend'
$frontendRoot = Join-Path $projectRoot 'frontend'
$python = 'D:\uv\python\cpython-3.12.13-windows-x86_64-none\python.exe'
if (-not (Test-Path -LiteralPath $python)) { $python = (Get-Command python -ErrorAction SilentlyContinue).Source }
if (-not $python) { throw 'Python runtime was not found.' }
$nodeCommand = Get-Command node -ErrorAction SilentlyContinue
if (-not $nodeCommand) { throw 'node.exe was not found.' }
$viteEntry = Join-Path $frontendRoot 'node_modules\vite\bin\vite.js'
if (-not (Test-Path -LiteralPath $viteEntry)) { throw 'Frontend dependencies are missing. Run npm install in frontend.' }
if (-not (Test-Path -LiteralPath (Join-Path $backendRoot '.deps'))) { throw 'Backend dependencies are missing. Run backend\install_backend.ps1.' }

$backendOut = Join-Path ([IO.Path]::GetTempPath()) 'semantic-backend-reload.out.log'
$backendErr = Join-Path ([IO.Path]::GetTempPath()) 'semantic-backend-reload.err.log'
Start-Process -FilePath $python -ArgumentList @('.\run.py', '--host', '127.0.0.1', '--port', [string]$BackendPort, '--reload') -WorkingDirectory $backendRoot -WindowStyle Hidden -RedirectStandardOutput $backendOut -RedirectStandardError $backendErr | Out-Null

Write-Host "FastAPI reload: http://127.0.0.1:$BackendPort" -ForegroundColor Green
Write-Host "FastAPI log: $backendErr" -ForegroundColor DarkGray
Write-Host "Vite HMR: http://127.0.0.1:$FrontendPort" -ForegroundColor Green
Write-Host 'Frontend stays in this terminal. Press Ctrl+C to stop Vite.' -ForegroundColor Yellow

Set-Location -LiteralPath $frontendRoot
& $nodeCommand.Source $viteEntry '--host' '127.0.0.1' '--port' ([string]$FrontendPort)
