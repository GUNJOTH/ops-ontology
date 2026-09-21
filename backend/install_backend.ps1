# 安装统一 Python 依赖（backend + system 共用 backend/.deps）。
$ErrorActionPreference = 'Stop'
$backendRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = 'D:\uv\python\cpython-3.12.13-windows-x86_64-none\python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw "未找到指定 Python：$python" }
$deps = Join-Path $backendRoot '.deps'
New-Item -ItemType Directory -Force -Path $deps | Out-Null
$lock = Join-Path $backendRoot 'requirements.lock'
if (Test-Path -LiteralPath $lock) {
    & 'D:\uv\bin\uv.exe' pip install --target $deps -r $lock
} else {
    Write-Warning 'requirements.lock 缺失；回退到 requirements.txt。请运行 uv pip compile 重新生成锁文件。'
    & 'D:\uv\bin\uv.exe' pip install --target $deps -r (Join-Path $backendRoot 'requirements.txt')
}
