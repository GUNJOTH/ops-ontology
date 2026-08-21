"""Product package boundary regression tests."""
from __future__ import annotations

from pathlib import Path

from semantic_packages import verify_package_registry

ROOT = Path(__file__).resolve().parents[2]


def test_core_and_thermal_packages_own_the_active_ontology_without_overlap() -> None:
    result = verify_package_registry(ontology_path=ROOT / "standards" / "v2" / "ontology.ttl")
    assert result["status"] == "PASS", result["failures"]
    assert result["packageCount"] == 2
    assert result["ownership"]["classes"] > 0
    assert result["ownership"]["objectProperties"] > 0
    assert result["ownership"]["datatypeProperties"] > 0


def test_package_registry_is_read_only_and_does_not_publish() -> None:
    result = verify_package_registry()
    assert result["sourceWrite"] is False
    assert result["formalPublication"] is False
