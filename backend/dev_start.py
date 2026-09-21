"""One-command local development launcher for the semantic engineering app.

Run from the project root with ``python backend/dev_start.py``.  This launcher
keeps both services in the same terminal, refuses duplicate ports, and avoids
PowerShell's case-insensitive ``PATH``/``Path`` environment collision.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = BACKEND_ROOT.parent
FRONTEND_ROOT = PROJECT_ROOT / "frontend"
LOCAL_ENV = BACKEND_ROOT / ".env"


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        item = line.strip()
        if not item or item.startswith("#") or "=" not in item:
            continue
        name, value = item.split("=", 1)
        name = name.strip()
        if name:
            os.environ[name] = value.strip().strip('"').strip("'")


def validate_agent_config() -> None:
    if not os.environ.get("RULE_AGENT_API_KEY"):
        print("[dev] RULE_AGENT_API_KEY is not configured; AI rule discovery will be unavailable", file=sys.stderr)
    if not os.environ.get("RULE_AGENT_BASE_URL"):
        os.environ["RULE_AGENT_BASE_URL"] = "https://www.ai.atyou.cn/v1"
    if not os.environ.get("RULE_AGENT_MODEL"):
        os.environ["RULE_AGENT_MODEL"] = "deepseek-v4-pro"


def normalized_environment() -> dict[str, str]:
    """Create a Windows-safe environment block with one key per name."""
    environment: dict[str, str] = {}
    original_names: dict[str, str] = {}
    for name, value in os.environ.items():
        normalized = name.lower()
        previous = original_names.get(normalized)
        if previous is not None:
            environment.pop(previous, None)
        original_names[normalized] = name
        environment[name] = value

    # Windows runtime variables need canonical names after de-duplication.
    for canonical in ("Path", "SystemRoot", "ComSpec", "TEMP", "TMP", "PATHEXT"):
        normalized = canonical.lower()
        value = next((v for k, v in os.environ.items() if k.lower() == normalized and v), None)
        if value:
            for key in list(environment):
                if key.lower() == normalized:
                    environment.pop(key, None)
            environment[canonical] = value
    return environment


def port_is_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.2)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def probe_http(url: str, timeout: float = 1.5) -> tuple[bool, int | None, str]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            body = response.read(4096).decode("utf-8", errors="replace")
            return True, int(response.status), body
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read(4096).decode("utf-8", errors="replace")
        except OSError:
            body = ""
        return False, int(exc.code), body
    except (urllib.error.URLError, TimeoutError, OSError):
        return False, None, ""


def existing_backend_ready(port: int) -> bool:
    ok, status, body = probe_http(f"http://127.0.0.1:{port}/api/health")
    if not ok or status != 200:
        return False
    try:
        return json.loads(body).get("status") == "ok"
    except json.JSONDecodeError:
        return False


def existing_frontend_ready(port: int) -> bool:
    ok, status, body = probe_http(f"http://127.0.0.1:{port}/")
    return ok and status == 200 and "id=\"root\"" in body


def terminate(process: subprocess.Popen[bytes] | None) -> None:
    if process is None or process.poll() is not None:
        return
    if os.name == "nt":
        taskkill = shutil.which("taskkill")
        if taskkill:
            subprocess.run(
                [taskkill, "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            try:
                process.wait(timeout=5)
                return
            except subprocess.TimeoutExpired:
                pass
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def wait_http(url: str, timeout: float = 20.0, process: subprocess.Popen[bytes] | None = None) -> tuple[bool, str]:
    deadline = time.monotonic() + timeout
    last_error = "not checked"
    while time.monotonic() < deadline:
        if process is not None and process.poll() is not None:
            return False, f"child exited with code {process.returncode}"
        try:
            with urllib.request.urlopen(url, timeout=1.5) as response:
                body = response.read(4096).decode("utf-8", errors="replace")
                return True, body
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = str(exc)
            time.sleep(0.25)
    return False, last_error


def main() -> int:
    parser = argparse.ArgumentParser(description="Start the semantic engineering development stack.")
    parser.add_argument("--backend-port", type=int, default=8001)
    parser.add_argument("--frontend-port", type=int, default=5173)
    parser.add_argument("--no-watch", action="store_true", help="Start FastAPI once instead of watching Python files.")
    parser.add_argument("--backend-only", action="store_true")
    parser.add_argument("--frontend-only", action="store_true")
    parser.add_argument("--check", action="store_true", help="Start services, run readiness checks, then exit.")
    args = parser.parse_args()
    if args.backend_only and args.frontend_only:
        parser.error("--backend-only and --frontend-only cannot be used together")

    load_env_file(LOCAL_ENV)
    validate_agent_config()
    ports = []
    if not args.frontend_only:
        ports.append(("FastAPI", args.backend_port))
    if not args.backend_only:
        ports.append(("Vite", args.frontend_port))
    reused_backend = not args.frontend_only and port_is_open(args.backend_port) and existing_backend_ready(args.backend_port)
    reused_frontend = not args.backend_only and port_is_open(args.frontend_port) and existing_frontend_ready(args.frontend_port)
    occupied = []
    if not args.frontend_only and port_is_open(args.backend_port) and not reused_backend:
        occupied.append(("FastAPI", args.backend_port))
    if not args.backend_only and port_is_open(args.frontend_port) and not reused_frontend:
        occupied.append(("Vite", args.frontend_port))
    if occupied:
        details = ", ".join(f"{name} {port}" for name, port in occupied)
        print(f"[dev] port already in use: {details}", file=sys.stderr)
        print("[dev] stop the existing service or choose another port; no duplicate process was started", file=sys.stderr)
        return 2
    if reused_backend:
        print(f"[dev] reusing healthy FastAPI: http://127.0.0.1:{args.backend_port}", flush=True)
    if reused_frontend:
        print(f"[dev] reusing healthy Vite: http://127.0.0.1:{args.frontend_port}", flush=True)

    environment = normalized_environment()
    python = sys.executable
    node = shutil.which("node")
    vite = FRONTEND_ROOT / "node_modules" / "vite" / "bin" / "vite.js"
    if not args.backend_only and (node is None or not vite.exists()):
        print("[dev] frontend dependencies are missing; run npm install in frontend", file=sys.stderr)
        return 2

    children: list[subprocess.Popen[bytes]] = []
    try:
        if not args.frontend_only and not reused_backend:
            # A readiness check must be finite.  Do not place the long-lived
            # file watcher behind --check; normal startup still uses it.
            backend_entry = "run.py" if args.no_watch or args.check else "watch_backend.py"
            backend = subprocess.Popen(
                [python, backend_entry, "--port", str(args.backend_port)],
                cwd=BACKEND_ROOT,
                env=environment,
            )
            children.append(backend)
            print(f"[dev] FastAPI: http://127.0.0.1:{args.backend_port} ({'single-run' if args.no_watch else 'hot reload'})", flush=True)
            ready, detail = wait_http(f"http://127.0.0.1:{args.backend_port}/api/health", process=backend)
            if not ready:
                print(f"[dev] FastAPI readiness failed: {detail}", file=sys.stderr)
                return 3
            try:
                import json

                health = json.loads(detail)
            except json.JSONDecodeError:
                print("[dev] FastAPI readiness failed: invalid health response", file=sys.stderr)
                return 3
            if health.get("status") != "ok":
                print(f"[dev] FastAPI health is not ok: {detail}", file=sys.stderr)
                return 3
            print("[dev] FastAPI readiness: ok", flush=True)

        if not args.backend_only and not reused_frontend:
            frontend = subprocess.Popen(
                [node, str(vite), "--host", "127.0.0.1", "--port", str(args.frontend_port)],
                cwd=FRONTEND_ROOT,
                env=environment,
            )
            children.append(frontend)
            print(f"[dev] Vite HMR: http://127.0.0.1:{args.frontend_port}", flush=True)
            ready, detail = wait_http(f"http://127.0.0.1:{args.frontend_port}/", process=frontend)
            if not ready or "root" not in detail:
                print(f"[dev] Vite readiness failed: {detail}", file=sys.stderr)
                return 3
            print("[dev] Vite readiness: ok", flush=True)

        if args.check:
            print("[dev] startup check passed", flush=True)
            return 0

        while True:
            exited = next((child for child in children if child.poll() is not None), None)
            if exited is not None:
                code = exited.returncode
                print(f"[dev] child exited with code {code}; stopping the remaining services", flush=True)
                return int(code or 0)
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("[dev] stopping", flush=True)
        return 0
    finally:
        for child in reversed(children):
            terminate(child)


if __name__ == "__main__":
    raise SystemExit(main())
