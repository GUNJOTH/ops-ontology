"""API 冒烟测试：元数据语义只读端点（直接调用处理器，只读本地结果库）。"""
from __future__ import annotations

import pytest
from app.core.config import METADATA_SQLITE_DB
from app.main import metadata_catalog, metadata_catalog_detail, metadata_export, metadata_summary
from fastapi import HTTPException

pytestmark = pytest.mark.api

requires_metadata_db = pytest.mark.skipif(
    not METADATA_SQLITE_DB.exists(), reason="元数据语义结果库不存在"
)


@requires_metadata_db
def test_metadata_summary_smoke() -> None:
    result = metadata_summary()
    assert result["sourceWrite"] is False
    assert result["formalPublication"] is False
    assert result["total"] >= 0
    assert "conceptTypes" in result
    assert "validation" in result


@requires_metadata_db
def test_metadata_catalog_smoke() -> None:
    result = metadata_catalog(page=1, page_size=10)
    assert result["sourceWrite"] is False
    assert result["total"] >= 0
    assert isinstance(result["items"], list)
    assert len(result["items"]) <= 10
    if result["items"]:
        assert "semanticId" in result["items"][0]


@requires_metadata_db
def test_metadata_catalog_detail_smoke() -> None:
    catalog = metadata_catalog(page=1, page_size=1)
    if not catalog["items"]:
        with pytest.raises(HTTPException) as error:
            metadata_catalog_detail("__empty_catalog_smoke__")
        assert error.value.status_code == 404
        return
    semantic_id = catalog["items"][0]["semanticId"]
    detail = metadata_catalog_detail(semantic_id)
    assert detail["item"]["semanticId"] == semantic_id
    assert detail["sourceWrite"] is False


@requires_metadata_db
def test_metadata_export_smoke() -> None:
    response = metadata_export(search="__nonexistent_smoke__")
    assert response.status_code == 200
    assert response.media_type.startswith("text/csv")
    # 内容是 utf-8-sig 编码，带 BOM。
    assert response.body.startswith(b"\xef\xbb\xbf")
