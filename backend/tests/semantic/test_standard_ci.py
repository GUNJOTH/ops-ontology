"""语义一致性测试：包装标准语义门禁 verify_standard_semantic_ci.py。

以子进程运行现有门禁（保持单一权威来源），断言其退出码为 0（报告 PASS）。
Canonical Semantic Model 尚未构建时自动跳过。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = BACKEND_ROOT.parent
SYSTEM_ROOT = PROJECT_ROOT / "system"
CANONICAL_DB = SYSTEM_ROOT / "data" / "canonical_semantic.sqlite3"
GATE_SCRIPT = SYSTEM_ROOT / "verify_standard_semantic_ci.py"

pytestmark = pytest.mark.semantic


@pytest.mark.skipif(not CANONICAL_DB.exists(), reason="Canonical Semantic Model 尚未构建")
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
