"""只读集成测试：Canonical RDF 读权威与兼容性回退标记。

口径（integration）：
- 用 ``client`` 夹具（TestClient 不进入 with 上下文）走完整 HTTP 栈；
- 直读真实只读数据源，缺库/空数据用 ``pytest.skip`` 而非断言失败；
- canonical 库正被后台投影写入（排它锁）时同样跳过，避免环境性误报；
- 一律断言 ``sourceWrite=False`` / ``formalPublication=False``。
"""
from __future__ import annotations

import sqlite3

import pytest

from app.core.config import CANONICAL_SEMANTICS_DB

pytestmark = pytest.mark.integration


def _canonical_readable() -> bool:
    """canonical 库存在且可读（未被后台投影的排它锁占用）。"""
    if not CANONICAL_SEMANTICS_DB.exists():
        return False
    try:
        connection = sqlite3.connect(
            f"file:{CANONICAL_SEMANTICS_DB.resolve()}?mode=ro", uri=True, timeout=2
        )
        try:
            connection.execute("SELECT 1 FROM canonical_projection_run LIMIT 1").fetchone()
            return True
        finally:
            connection.close()
    except sqlite3.OperationalError:
        return False


def test_source_of_truth_reports_explicit_cutover_coverage(client) -> None:
    if not _canonical_readable():
        pytest.skip("Canonical RDF 正在投影或被占用，跳过只读冒烟")
    response = client.get("/api/semantic/source-of-truth")
    if response.status_code in {404, 503}:
        pytest.skip("Canonical RDF 尚未完成投影或端点不可用")
    assert response.status_code == 200
    payload = response.json()
    assert payload["authority"] == "Canonical RDF Dataset"
    assert payload["status"] in {"active", "partial"}
    assert 0 <= payload["coverage"] <= 1
    assert payload["sourceWrite"] is False
    assert payload["formalPublication"] is False


def test_unprojected_device_is_explicitly_labelled_compatibility_read(client) -> None:
    if not _canonical_readable():
        pytest.skip("Canonical RDF 正在投影或被占用，跳过只读冒烟")
    devices = client.get("/api/unified-devices", params={"page": 1, "page_size": 1})
    assert devices.status_code == 200
    rows = devices.json()["rows"]
    if not rows:
        pytest.skip("统一设备库为空，无设备可验证兼容性回退标记")
    device_id = rows[0]["unifiedDeviceId"]
    response = client.get(f"/api/world-model/device/{device_id}/context")
    assert response.status_code == 200
    payload = response.json()
    assert payload["semanticSourceOfTruth"] in {
        "canonical-rdf-dataset",
        "relational-runtime-pending-canonical",
    }
    if payload["semanticSourceOfTruth"] != "canonical-rdf-dataset":
        assert payload["canonicalRunId"] is None
