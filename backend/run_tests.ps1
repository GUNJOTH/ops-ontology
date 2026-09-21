# Standardized test entrypoint: uv-managed environment + pytest.
#
# Usage (any pytest args are forwarded as-is):
#   .\backend\run_tests.ps1                    # full suite
#   .\backend\run_tests.ps1 -m "unit"          # marker filter
#   .\backend\run_tests.ps1 -m "not semantic"  # skip semantic gate
#
# Steps performed automatically:
#   1) locate uv and the uv-managed CPython 3.12;
#   2) point the uv cache at backend/.uv-cache (writable, gitignored) so that
#      restricted sandboxes without a shared writable cache still work;
#   3) uv pip install --target .deps from requirements.lock (runtime + test deps,
#      idempotent); 这是 backend 与 system 共用的统一 Python 依赖；锁文件缺失时回退到 requirements.txt + requirements-dev.txt；
#   4) ruff check 门禁：lint 不干净则失败并中止（S1 门禁）；
#   5) set PYTHONPATH / PYTHONNOUSERSITE, then run pytest.

$backendRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = Split-Path -Parent $backendRoot
$systemRoot = Join-Path $projectRoot 'system'

# Locate uv and the uv-managed Python.
$uv = 'D:\uv\bin\uv.exe'
if (-not (Test-Path -LiteralPath $uv)) { $uv = (Get-Command uv -ErrorAction SilentlyContinue).Source }
if (-not $uv) { Write-Error 'uv not found; install uv first.'; exit 1 }

$python = 'D:\uv\python\cpython-3.12.13-windows-x86_64-none\python.exe'
if (-not (Test-Path -LiteralPath $python)) { $python = (Get-Command python -ErrorAction SilentlyContinue).Source }
if (-not $python) { Write-Error 'Python 3.12 not found; install it via uv first.'; exit 1 }

# Point the uv cache at a project-local writable directory.
$uvCache = Join-Path $backendRoot '.uv-cache'
New-Item -ItemType Directory -Force -Path $uvCache | Out-Null
$env:UV_CACHE_DIR = $uvCache

# Vendor runtime + test deps into .deps (idempotent). Install from the compiled
# lock so the exact resolved set is reproducible; regenerate it with:
#   uv pip compile requirements.txt requirements-dev.txt -o requirements.lock --no-header
$deps = Join-Path $backendRoot '.deps'
$lock = Join-Path $backendRoot 'requirements.lock'
$installArgs = @('pip', 'install', '--target', $deps)
if (Test-Path -LiteralPath $lock) {
    $installArgs += @('-r', $lock)
} else {
    Write-Warning 'requirements.lock 缺失；回退到 requirements.txt + requirements-dev.txt。请运行 uv pip compile 重新生成锁文件。'
    $installArgs += @('-r', (Join-Path $backendRoot 'requirements.txt'), '-r', (Join-Path $backendRoot 'requirements-dev.txt'))
}
& $uv @installArgs 2>&1
if ($LASTEXITCODE -ne 0) { Write-Error "uv pip install failed (exit $LASTEXITCODE)."; exit $LASTEXITCODE }

# Lint gate (S1): ruff check must be clean before running tests.
$ruff = Join-Path $deps 'bin\ruff.exe'
if (Test-Path -LiteralPath $ruff) {
    & $ruff check (Join-Path $backendRoot 'app') (Join-Path $backendRoot 'tests') $systemRoot
    if ($LASTEXITCODE -ne 0) { Write-Error "ruff check failed (exit $LASTEXITCODE)；请先修复 lint 再运行测试。"; exit $LASTEXITCODE }
} else {
    Write-Warning 'ruff 未安装（requirements-dev.txt 缺失）；跳过 lint 门禁。'
}

# Maintainability budget: fail fast when module/script scale budgets are exceeded.
& $python (Join-Path $systemRoot 'verify_maintainability.py') --strict
if ($LASTEXITCODE -ne 0) { Write-Error 'maintainability budget failed (exit ' + $LASTEXITCODE + ').'; exit $LASTEXITCODE }

# Run pytest. Include system so the shared pipeline/semantic contracts are
# tested by the same entrypoint as the backend.
$env:PYTHONPATH = "$deps;$backendRoot;$systemRoot"
# Isolate user site-packages to avoid pollution (e.g. a global pytest_asyncio).
$env:PYTHONNOUSERSITE = '1'

Push-Location $backendRoot
try {
    & $python -m pytest @args
    exit $LASTEXITCODE
} finally {
    Pop-Location
}
