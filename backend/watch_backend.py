"""Windows-friendly development watcher for the FastAPI backend.

Uvicorn's multiprocessing reloader can fail in restricted Windows sessions
while creating its named pipe. This watcher keeps the application process
single-process and restarts it when files under ``app`` change.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
WATCH_DIR = ROOT / "app"


def snapshot() -> dict[Path, int]:
    return {
        path: path.stat().st_mtime_ns
        for path in WATCH_DIR.rglob("*.py")
        if path.is_file()
    }


def stop_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def main() -> None:
    parser = argparse.ArgumentParser(description="Watch and restart the semantic FastAPI backend.")
    parser.add_argument("--port", type=int, default=8001)
    parser.add_argument("--interval", type=float, default=0.5)
    args = parser.parse_args()

    previous = snapshot()
    process: subprocess.Popen[bytes] | None = None
    try:
        while True:
            if process is None or process.poll() is not None:
                if process is not None:
                    print(f"[watch] backend exited with code {process.returncode}; restarting", flush=True)
                    if process.returncode == 3:
                        print("[watch] stopping after bind/startup failure; check the configured port before retrying", flush=True)
                        break
                process = subprocess.Popen(
                    [sys.executable, str(ROOT / "run.py"), "--port", str(args.port)],
                    cwd=str(ROOT),
                )
                print(f"[watch] backend running on http://127.0.0.1:{args.port}", flush=True)

            time.sleep(args.interval)
            current = snapshot()
            if current != previous:
                changed = sorted({*previous, *current} - {path for path in previous if path in current and previous[path] == current[path]})
                print(f"[watch] change detected: {', '.join(str(path.relative_to(ROOT)) for path in changed)}", flush=True)
                stop_process(process)
                process = None
                previous = current
    except KeyboardInterrupt:
        print("[watch] stopping", flush=True)
    finally:
        if process is not None:
            stop_process(process)


if __name__ == "__main__":
    main()
