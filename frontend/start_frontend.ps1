param(
  [int]$Port = 5173
)

$ErrorActionPreference = 'Stop'
$frontendRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$nodeCommand = Get-Command node -ErrorAction SilentlyContinue

if (-not $nodeCommand) {
  throw '未找到 node.exe。请先安装 Node.js 20.19+ 或把 Node.js 加入 PATH。'
}

$viteEntry = Join-Path $frontendRoot 'node_modules\vite\bin\vite.js'
if (-not (Test-Path -LiteralPath $viteEntry)) {
  throw '前端依赖尚未安装，请先在当前目录执行 npm install。'
}

Set-Location -LiteralPath $frontendRoot
& $nodeCommand.Source $viteEntry '--host' '127.0.0.1' '--port' ([string]$Port)
