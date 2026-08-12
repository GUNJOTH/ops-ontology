param(
  [int]$Port = 8000,
  [switch]$Reload
)

$ErrorActionPreference = 'Stop'
$backendRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = 'D:\uv\python\cpython-3.12.13-windows-x86_64-none\python.exe'
if (-not (Test-Path -LiteralPath $python)) { $python = (Get-Command python -ErrorAction SilentlyContinue).Source }
if (-not $python) { throw '未找到 Python 运行时。' }

Set-Location -LiteralPath $backendRoot
$arguments = @('.\run.py', '--port', [string]$Port)
if ($Reload) { $arguments += '--reload' }
& $python @arguments
