"""API 测试：/api/health 的 ok/degraded 分支。"""
from __future__ import annotations

import pytest

from app.main import health

pytestmark = pytest.mark.api


def test_health_ok_when_required_semantic_artifacts_exist(monkeypatch, tmp_dir) -> None:
    sqlite = tmp_dir / "semantic_workflow.sqlite3"
    duck = tmp_dir / "semantic_analytics_v155.duckdb"
    canonical = tmp_dir / "canonical_semantic.sqlite3"
    sqlite.write_text("", encoding="utf-8")
    duck.write_text("", encoding="utf-8")
    canonical.write_text("", encoding="utf-8")
    monkeypatch.setattr("app.main.SQLITE_DB", sqlite)
    monkeypatch.setattr("app.main.DUCKDB_DB", duck)
    monkeypatch.setattr("app.main.CANONICAL_SEMANTICS_DB", canonical)
    payload = health()
    assert payload["status"] == "ok"
    assert payload["sqlite"] is True
    assert payload["duckdb"] is True
    assert payload["canonicalRdf"] is True
    assert payload["sourceWrite"] is False
    assert payload["formalPublication"] is False
    assert isinstance(payload["canonicalRdf"], bool)
    assert payload["activeRelease"] is None or isinstance(payload["activeRelease"], dict)
    assert isinstance(payload["metricSeries"], int)


def test_health_degraded_when_databases_missing(monkeypatch, tmp_dir) -> None:
    monkeypatch.setattr("app.main.SQLITE_DB", tmp_dir / "missing.sqlite3")
    monkeypatch.setattr("app.main.DUCKDB_DB", tmp_dir / "missing.duckdb")
    payload = health()
    assert payload["status"] == "degraded"
    assert payload["sqlite"] is False
    assert payload["duckdb"] is False
    assert payload["sourceWrite"] is False
    assert payload["formalPublication"] is False
    assert isinstance(payload["canonicalRdf"], bool)
    assert payload["activeRelease"] is None or isinstance(payload["activeRelease"], dict)
    assert isinstance(payload["metricSeries"], int)
