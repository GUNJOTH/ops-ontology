"""Tests for the side-effect-free maintainability guard."""
from __future__ import annotations

from pathlib import Path

import cli
import verify_maintainability


def test_current_repository_is_within_maintainability_budget() -> None:
    report = verify_maintainability.audit_repository()
    assert report["status"] == "PASS", report["violations"]


def test_budget_detects_growth() -> None:
    root = Path(__file__).resolve().parents[2]
    budget = verify_maintainability.load_budget(root / "contracts" / "maintainability_budget.json")
    budget["max"]["system_scripts"] = 0
    report = verify_maintainability.audit_repository(root, budget)
    assert any(item["metric"] == "system_scripts" for item in report["violations"])


def test_cli_registry_exposes_maintenance_command() -> None:
    assert "verify_maintainability.py" in cli._all_scripts()
