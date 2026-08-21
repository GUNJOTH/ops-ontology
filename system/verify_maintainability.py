"""Check code-maintenance budgets without opening project databases.

The command is intentionally stdlib-only and side-effect free. It makes
maintenance debt visible in local runs and CI while allowing the existing
semantic runtime to be refactored in small, reviewable batches.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
BUDGET_FILE = ROOT / "contracts" / "maintainability_budget.json"
PYTHON_SUFFIX = "*.py"


def _python_files(path: Path, *, recursive: bool = True) -> list[Path]:
    iterator = path.rglob(PYTHON_SUFFIX) if recursive else path.glob(PYTHON_SUFFIX)
    return sorted(item for item in iterator if item.is_file() and "__pycache__" not in item.parts)


def _line_count(path: Path) -> int:
    return sum(1 for _ in path.open("r", encoding="utf-8", errors="replace"))


def _matching_files(files: Iterable[Path], pattern: str) -> list[Path]:
    expression = re.compile(pattern)
    return [path for path in files if expression.search(path.read_text(encoding="utf-8", errors="replace"))]


def load_budget(path: Path = BUDGET_FILE) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("max"), dict) or not isinstance(payload.get("min"), dict):
        raise ValueError(f"invalid maintainability budget: {path}")
    return payload


def audit_repository(root: Path = ROOT, budget: dict[str, Any] | None = None) -> dict[str, Any]:
    budget = budget or load_budget(root / "contracts" / "maintainability_budget.json")
    backend_root = root / "backend"
    system_root = root / "system"
    backend_files = _python_files(backend_root / "app")
    system_scripts = _python_files(system_root, recursive=False)
    pipeline_files = _matching_files(system_scripts, r"(?:from\s+pipeline|import\s+pipeline)")
    sqlite_files = _matching_files(system_scripts, r"sqlite3\.connect")
    duckdb_files = _matching_files(system_scripts, r"duckdb\.connect")
    backend_tests = _python_files(backend_root / "tests")
    system_tests = _python_files(system_root / "tests")

    backend_main = root / "backend" / "app" / "main.py"
    largest_backend = max(((_line_count(path), path) for path in backend_files), default=(0, backend_main))
    largest_system = max(((_line_count(path), path) for path in system_scripts), default=(0, system_root))
    metrics = {
        "backend_main_lines": _line_count(backend_main),
        "largest_backend_module_lines": largest_backend[0],
        "largest_backend_module": largest_backend[1].relative_to(root).as_posix(),
        "largest_system_script_lines": largest_system[0],
        "largest_system_script": largest_system[1].relative_to(root).as_posix(),
        "system_scripts": len(system_scripts),
        "pipeline_users": len(pipeline_files),
        "direct_sqlite_connect_scripts": len(sqlite_files),
        "direct_duckdb_connect_scripts": len(duckdb_files),
        "backend_test_files": len(backend_tests),
        "system_test_files": len(system_tests),
    }

    violations: list[dict[str, Any]] = []
    for key, limit in budget["max"].items():
        if key in metrics and metrics[key] > int(limit):
            violations.append({"metric": key, "actual": metrics[key], "limit": int(limit), "kind": "max"})
    for key, limit in budget["min"].items():
        if key in metrics and metrics[key] < int(limit):
            violations.append({"metric": key, "actual": metrics[key], "limit": int(limit), "kind": "min"})

    return {
        "status": "PASS" if not violations else "FAIL",
        "root": str(root),
        "budgetVersion": budget.get("version"),
        "metrics": metrics,
        "violations": violations,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Check repository maintainability budgets without touching runtime data")
    parser.add_argument("--strict", action="store_true", help="return non-zero when a budget is violated")
    parser.add_argument("--json", action="store_true", dest="as_json", help="emit machine-readable JSON only")
    args = parser.parse_args()
    report = audit_repository()
    if args.as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"maintainability: {report['status']}")
        for key, value in report["metrics"].items():
            print(f"  {key}: {value}")
        for item in report["violations"]:
            print(f"  violation: {item['metric']} actual={item['actual']} limit={item['limit']}")
    return 1 if args.strict and report["status"] != "PASS" else 0


if __name__ == "__main__":
    raise SystemExit(main())
