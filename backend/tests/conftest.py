"""共享测试配置与夹具。

在导入任何 ``app.*`` 之前，把后端包目录和 vendor 依赖目录加入 ``sys.path``。
"""
from __future__ import annotations

import shutil
import sqlite3
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
DEPS_DIR = BACKEND_ROOT / ".deps"

# 先放 backend（保证 app 解析到本项目源码），再放 .deps（第三方依赖）。
for _path in (str(BACKEND_ROOT), str(DEPS_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)


@pytest.fixture
def tmp_dir() -> Iterator[Path]:
    """仓库内、宽松权限（0o777）的临时目录。

    与 pytest 内置 ``tmp_path`` / ``tempfile.mkdtemp`` 的差异：后两者以 0o700
    建目录，在受限的 Windows 沙箱下会被拒绝（owner-only 权限位导致连创建者
    都无法枚举）。这里显式用 0o777，普通环境与沙箱环境均可运行。
    """
    root = BACKEND_ROOT / ".test_tmp"
    root.mkdir(mode=0o777, exist_ok=True)
    path = root / f"{uuid.uuid4().hex[:16]}"
    path.mkdir(mode=0o777)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


AUDIT_EVENT_DDL = """
CREATE TABLE audit_event (
  event_id INTEGER PRIMARY KEY AUTOINCREMENT,
  entity_type TEXT NOT NULL,
  entity_id TEXT NOT NULL,
  event_type TEXT NOT NULL,
  actor TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  event_at TEXT NOT NULL
);
"""


@pytest.fixture
def tmp_workflow_db() -> sqlite3.Connection:
    """内存工作流 SQLite，作为写路径测试的种子点。

    具体表结构与行种子由 data/api 套件按需加入，事务、审计、幂等键等
    写安全边界都只落在这个临时库上。
    """
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture
def audit_db(tmp_workflow_db) -> sqlite3.Connection:
    """带最小 audit_event 表的内存库，用于审计与幂等键测试。"""
    tmp_workflow_db.executescript(AUDIT_EVENT_DDL)
    tmp_workflow_db.commit()
    return tmp_workflow_db
