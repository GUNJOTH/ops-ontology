"""Connection factories and FastAPI dependency hooks.

The factories centralize SQLite pragmas and source-read-only boundaries.  The
schema initializer hook is retained only for controlled compatibility tests;
the application leaves it disabled and runs migrations at startup.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from fastapi import Depends, HTTPException

from .config import (
    CANONICAL_SEMANTICS_DB,
    DUCKDB_DB,
    IDENTITY_RESULT_ROOT,
    METADATA_DUCKDB_DB,
    METADATA_SQLITE_DB,
    SQLITE_DB,
    UNIFIED_SEMANTICS_DB,
)


WorkflowSchemaInitializer = Callable[[sqlite3.Connection], None]
_workflow_schema_initializer: WorkflowSchemaInitializer | None = None


def configure_workflow_schema(initializer: WorkflowSchemaInitializer | None) -> None:
    global _workflow_schema_initializer
    _workflow_schema_initializer = initializer


def _configure_sqlite(connection: sqlite3.Connection, *, writable: bool) -> sqlite3.Connection:
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=30000")
    if writable:
        connection.execute("PRAGMA foreign_keys=ON")
    else:
        connection.execute("PRAGMA query_only=ON")
    return connection


def sqlite_connection() -> sqlite3.Connection:
    if not SQLITE_DB.exists():
        raise HTTPException(status_code=503, detail="SQLite 工作流数据库不存在")
    connection = _configure_sqlite(sqlite3.connect(str(SQLITE_DB), timeout=30), writable=True)
    if _workflow_schema_initializer is not None:
        _workflow_schema_initializer(connection)
    return connection


def raw_workflow_connection() -> sqlite3.Connection:
    """Open the writable workflow DB without request-time schema migration."""
    if not SQLITE_DB.exists():
        raise HTTPException(status_code=503, detail="SQLite 工作流数据库不存在")
    return _configure_sqlite(sqlite3.connect(str(SQLITE_DB), timeout=30), writable=True)


def get_sqlite() -> Iterator[sqlite3.Connection]:
    connection = sqlite_connection()
    try:
        yield connection
    finally:
        connection.close()


def latest_identity_result_db() -> Path:
    configured = __import__("os").getenv("IDENTITY_RESULT_DB", "").strip()
    if configured:
        path = Path(configured)
        if path.exists():
            return path
    for directory in sorted(IDENTITY_RESULT_ROOT.glob("identity-layer-v1-*/"), reverse=True):
        database = directory / "identity_semantics.sqlite3"
        if (directory / "manifest.json").exists() and database.exists():
            return database
    raise HTTPException(status_code=503, detail="统一设备身份结果库不存在")


def identity_result_connection() -> sqlite3.Connection:
    path = latest_identity_result_db().resolve()
    return _configure_sqlite(sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30), writable=False)


def unified_semantics_connection() -> sqlite3.Connection:
    if not UNIFIED_SEMANTICS_DB.exists():
        raise HTTPException(status_code=503, detail="统一设备语义覆盖层不存在，请先构建本地关系层")
    return _configure_sqlite(
        sqlite3.connect(f"file:{UNIFIED_SEMANTICS_DB.resolve()}?mode=ro", uri=True, timeout=30),
        writable=False,
    )


def unified_semantics_write_connection() -> sqlite3.Connection:
    if not UNIFIED_SEMANTICS_DB.exists():
        raise HTTPException(status_code=503, detail="统一设备语义覆盖层不存在，请先构建本地关系层")
    return _configure_sqlite(sqlite3.connect(str(UNIFIED_SEMANTICS_DB), timeout=30), writable=True)


def canonical_semantics_connection() -> sqlite3.Connection:
    if not CANONICAL_SEMANTICS_DB.exists():
        raise HTTPException(status_code=503, detail="Canonical Semantic Model 尚未构建，请先运行标准投影")
    return _configure_sqlite(
        sqlite3.connect(f"file:{CANONICAL_SEMANTICS_DB.resolve()}?mode=ro", uri=True, timeout=30),
        writable=False,
    )


def duckdb_connection() -> Any:
    if not DUCKDB_DB.exists():
        raise HTTPException(status_code=503, detail="DuckDB 分析库不存在")
    import duckdb

    return duckdb.connect(str(DUCKDB_DB), read_only=True)


def metadata_sqlite_connection() -> sqlite3.Connection:
    if not METADATA_SQLITE_DB.exists():
        raise HTTPException(status_code=503, detail="元数据语义 SQLite 不存在")
    return _configure_sqlite(
        sqlite3.connect(f"file:{METADATA_SQLITE_DB.resolve()}?mode=ro", uri=True, timeout=30),
        writable=False,
    )


def metadata_duckdb_connection() -> Any:
    if not METADATA_DUCKDB_DB.exists():
        raise HTTPException(status_code=503, detail="元数据语义 DuckDB 不存在")
    import duckdb

    return duckdb.connect(str(METADATA_DUCKDB_DB), read_only=True)
