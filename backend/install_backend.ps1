$ErrorActionPreference = 'Stop'
$backendRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = 'D:\uv\python\cpython-3.12.13-windows-x86_64-none\python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw "未找到指定 Python：$python" }
$deps = Join-Path $backendRoot '.deps'
New-Item -ItemType Directory -Force -Path $deps | Out-Null
& 'D:\uv\bin\uv.exe' pip install --target $deps -r (Join-Path $backendRoot 'requirements.txt')
