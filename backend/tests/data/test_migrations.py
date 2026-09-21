"""数据测试：迁移编排与 cleaning 迁移的幂等性。"""
from __future__ import annotations

import pytest
from app.main import ensure_cleaning_schema
from app.migrations.workflow_schema import migrate_workflow_schema

pytestmark = pytest.mark.unit


def test_migrate_workflow_schema_runs_steps_in_order(tmp_workflow_db) -> None:
    calls: list[str] = []

    def step(name: str):
        def apply(connection) -> None:
            calls.append(name)

        return apply

    migrate_workflow_schema(tmp_workflow_db, step("a"), step("b"))
    assert calls == ["a", "b"]
    assert tmp_workflow_db.execute(
        "SELECT count(*) AS c FROM semantic_schema_migration"
    ).fetchone()["c"] == 2


def test_migrate_workflow_schema_skips_applied_version(tmp_workflow_db) -> None:
    calls: list[str] = []

    def apply(connection) -> None:
        calls.append("once")

    migrate_workflow_schema(tmp_workflow_db, ("test-v1", apply))
    migrate_workflow_schema(tmp_workflow_db, ("test-v1", apply))
    assert calls == ["once"]


def test_ensure_cleaning_schema_is_idempotent(tmp_workflow_db) -> None:
    ensure_cleaning_schema(tmp_workflow_db)
    first_rules = tmp_workflow_db.execute(
        "SELECT count(*) AS c FROM cleaning_rule_registry"
    ).fetchone()["c"]
    first_runs = tmp_workflow_db.execute(
        "SELECT count(*) AS c FROM cleaning_run"
    ).fetchone()["c"]

    # 第二次执行不得报错，也不得重复插入初始规则。
    ensure_cleaning_schema(tmp_workflow_db)
    second_rules = tmp_workflow_db.execute(
        "SELECT count(*) AS c FROM cleaning_rule_registry"
    ).fetchone()["c"]
    second_runs = tmp_workflow_db.execute(
        "SELECT count(*) AS c FROM cleaning_run"
    ).fetchone()["c"]

    assert first_rules == second_rules == 3
    assert first_runs == second_runs == 3


def test_migrate_workflow_schema_with_cleaning_is_re_runnable(tmp_workflow_db) -> None:
    migrate_workflow_schema(tmp_workflow_db, ensure_cleaning_schema)
    migrate_workflow_schema(tmp_workflow_db, ensure_cleaning_schema)
    assert (
        tmp_workflow_db.execute(
            "SELECT count(*) AS c FROM cleaning_rule_registry"
        ).fetchone()["c"]
        == 3
    )
