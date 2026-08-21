"""Start the semantic FastAPI service without going through npm/cmd wrappers."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEPS = ROOT / ".deps"
SYSTEM_ROOT = ROOT.parent / "system"
if DEPS.exists():
    sys.path.insert(0, str(DEPS))
sys.path.insert(0, str(SYSTEM_ROOT))
sys.path.insert(0, str(ROOT))

import uvicorn  # type: ignore  # noqa: E402 - 先引导 .deps/ROOT 到 sys.path 后再导入第三方包

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--reload", action="store_true", help="开发模式：代码变更后自动重启")
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args()
    uvicorn.run(
        "app.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        reload_dirs=[str(ROOT / "app")] if args.reload else None,
        log_level=args.log_level,
    )
