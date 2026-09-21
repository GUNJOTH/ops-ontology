"""语义一致性测试：包装标准语义门禁 verify_standard_semantic_ci.py。

以子进程运行现有门禁（保持单一权威来源），断言其退出码为 0（报告 PASS）。
Canonical Semantic Model 尚未构建（或处于构建中/残缺状态）时自动跳过。
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = BACKEND_ROOT.parent
SYSTEM_ROOT = PROJECT_ROOT / "system"
CANONICAL_DB = SYSTEM_ROOT / "data" / "canonical_semantic.sqlite3"
GATE_SCRIPT = SYSTEM_ROOT / "verify_standard_semantic_ci.py"

# 完整 Canonical Semantic Model 的关键表（由 build_canonical_semantic_model.py
# 在同一 schema 块中创建）。缺失任一表即视为未构建/构建中，门禁不应运行。
_REQUIRED_TABLES = {"canonical_projection_run", "canonical_statement", "canonical_graph"}

pytestmark = pytest.mark.semantic


def _canonical_model_built() -> bool:
    """库存在且关键表齐全才算已构建；残缺/构建中的库视为未构建。"""
    if not CANONICAL_DB.exists():
        return False
    try:
        connection = sqlite3.connect(str(CANONICAL_DB))
        try:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        finally:
            connection.close()
    except sqlite3.Error:
        return False
    return _REQUIRED_TABLES <= tables


@pytest.mark.skipif(not _canonical_model_built(), reason="Canonical Semantic Model 尚未完整构建")
def test_standard_semantic_ci_gate_passes() -> None:
    env = dict(os.environ)
    parts = [str(BACKEND_ROOT / ".deps"), str(BACKEND_ROOT), str(SYSTEM_ROOT)]
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = os.pathsep.join(parts + ([existing] if existing else []))

    result = subprocess.run(
        [sys.executable, str(GATE_SCRIPT)],
        cwd=str(SYSTEM_ROOT),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
    )
    detail = ((result.stdout or "") + "\n" + (result.stderr or "")).strip()
    if result.returncode != 0 and "database is locked" in detail:
        pytest.skip("canonical 库被并发投影占用，跳过语义门禁重跑")
    assert result.returncode == 0, (
        f"语义门禁失败（exit {result.returncode}）：\n{detail[-4000:]}"
    )
