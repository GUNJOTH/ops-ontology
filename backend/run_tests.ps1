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
#   3) uv pip install --target .deps (runtime + test deps, idempotent);
#   4) set PYTHONPATH / PYTHONNOUSERSITE, then run pytest.

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

# Vendor runtime + test deps into .deps (idempotent). Merge uv's stderr so that
# informational lines (e.g. "Using CPython ...") are not treated as errors.
$deps = Join-Path $backendRoot '.deps'
& $uv pip install --target $deps `
    -r (Join-Path $backendRoot 'requirements.txt') `
    -r (Join-Path $backendRoot 'requirements-dev.txt') 2>&1
if ($LASTEXITCODE -ne 0) { Write-Error "uv pip install failed (exit $LASTEXITCODE)."; exit $LASTEXITCODE }

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
