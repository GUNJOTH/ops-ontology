"""集成测试共享夹具。

口径：TestClient 不进入 ``with`` 上下文，避免触发 startup lifespan
迁移去写真实工作流库；写路径用例把 ``app.core.db.SQLITE_DB`` 重定向到临时文件库，
绝不触碰 ``system/data``。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from app.core import db as core_db
from app.main import app, ensure_cleaning_schema, ensure_review_sample_schema
from app.migrations.workflow_schema import migrate_workflow_schema
from fastapi.testclient import TestClient

BACKEND_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = BACKEND_ROOT.parent
SQLITE_SCHEMA = PROJECT_ROOT / "system" / "sqlite_schema.sql"


@pytest.fixture
def client() -> TestClient:
    """不触发 lifespan 的 TestClient（完整 HTTP 栈，但不跑 startup 迁移）。"""
    return TestClient(app)


def build_workflow_schema(connection: sqlite3.Connection) -> None:
    """在空文件库上重放生产 schema：基础 schema + 后端增量迁移。

    直接执行 ``system/sqlite_schema.sql``（复用既有定义，不复制），再跑与 startup
    相同的 ``migrate_workflow_schema(ensure_review_sample_schema, ensure_cleaning_schema)``。
    """
    connection.executescript(SQLITE_SCHEMA.read_text(encoding="utf-8"))
    migrate_workflow_schema(connection, ensure_review_sample_schema, ensure_cleaning_schema)
    connection.commit()


@pytest.fixture
def isolated_workflow_db(tmp_dir, monkeypatch) -> Path:
    """临时工作流文件库 + 生产 schema，并把 app 的工作流连接重定向到它。"""
    db_path = Path(tmp_dir) / "workflow.sqlite3"
    connection = sqlite3.connect(str(db_path))
    connection.execute("PRAGMA foreign_keys=ON")
    build_workflow_schema(connection)
    connection.close()
    monkeypatch.setattr(core_db, "SQLITE_DB", db_path)
    return db_path
