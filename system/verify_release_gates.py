"""Aggregate the local gates required before preparing a Semantic Release."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parent
BACKEND_ROOT = PROJECT_ROOT / "backend"


def run_gate(script: str) -> dict[str, Any]:
    env = os.environ.copy()
    paths = [str(BACKEND_ROOT / ".deps"), str(BACKEND_ROOT)]
    if env.get("PYTHONPATH"):
        paths.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(paths)
    completed = subprocess.run(
        [sys.executable, str(ROOT / script)],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode != 0:
        return {"status": "FAIL", "script": script, "exitCode": completed.returncode, "outputTail": completed.stdout[-2000:]}
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError:
        return {"status": "FAIL", "script": script, "exitCode": completed.returncode, "reason": "门禁输出不是 JSON", "outputTail": completed.stdout[-2000:]}


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Canonical RDF Semantic Release gates")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    checks = {
        "canonical": run_gate("verify_canonical_semantic_model.py"),
        "standardCi": run_gate("verify_standard_semantic_ci.py"),
        "sourceTruth": run_gate("verify_source_truth_cutover.py"),
    }
    status = "PASS" if all(item.get("status") == "PASS" for item in checks.values()) else "BLOCKED"
    canonical_run = ((checks["canonical"].get("latestRun") or {}).get("run_id") or checks["sourceTruth"].get("canonicalRunId"))
    payload = {
        "schemaVersion": "semantic-release-gates-v1",
        "status": status,
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "canonicalRunId": canonical_run,
        "checks": checks,
        "sourceWrite": False,
        "formalPublication": False,
    }
    output = args.output or (ROOT / "reports" / f"semantic-release-gates-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json")
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"status": status, "report": str(output), "canonicalRunId": canonical_run, "sourceWrite": False, "formalPublication": False}, ensure_ascii=False, indent=2))
    raise SystemExit(0 if status == "PASS" else 2)


if __name__ == "__main__":
    main()
