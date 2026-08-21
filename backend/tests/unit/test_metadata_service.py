"""单元测试：app.domains.metadata.service 的过滤与行映射。"""
from __future__ import annotations

import sqlite3

import pytest

from app.domains.metadata.service import metadata_filters, metadata_row_payload

pytestmark = pytest.mark.unit


def test_filters_default_no_constraints() -> None:
    where, params = metadata_filters(None, None, None, None, None)
    assert where == "1=1"
    assert params == []


def test_filters_ignore_all_sentinel() -> None:
    where, params = metadata_filters("all", "", "all", "all", "all")
    assert where == "1=1"
    assert params == []


def test_filters_single_concept_type() -> None:
    where, params = metadata_filters("Device", None, None, None, None)
    assert "concept_type = ?" in where
    assert params == ["Device"]


def test_filters_search_expands_six_like_fields() -> None:
    where, params = metadata_filters(None, "pump", None, None, None)
    assert "semantic_id LIKE ?" in where
    assert "parent_or_table LIKE ?" in where
    assert params == ["%pump%"] * 6


def test_filters_source_schema_like() -> None:
    where, params = metadata_filters(None, None, None, None, "HD_SAAS")
    assert "source_schemas LIKE ?" in where
    assert params == ["%HD_SAAS%"]


def test_filters_combined() -> None:
    where, params = metadata_filters("Device", "pump", "cat", "approved", "HD_SAAS")
    assert where.count(" AND ") == 5
    assert params == [
        "Device",
        "cat",
        "approved",
        "%HD_SAAS%",
        "%pump%",
        "%pump%",
        "%pump%",
        "%pump%",
        "%pump%",
        "%pump%",
    ]


def test_row_payload_maps_snake_to_camel() -> None:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    row = connection.execute(
        "SELECT ? AS semantic_id, ? AS dictionary_version, ? AS concept_type, "
        "? AS semantic_key, ? AS canonical_name, ? AS semantic_label_candidate, "
        "? AS description, ? AS data_type, ? AS length, ? AS required, ? AS domain_id, "
        "? AS parent_or_table, ? AS source_schemas, ? AS cross_schema_status, "
        "? AS ai_category, ? AS ai_confidence, ? AS ai_reason, ? AS semantic_status, "
        "? AS evidence, ? AS loaded_at",
        (
            "id1", "v1", "Device", "key1", "Name", "Label", "Desc", "TEXT", 10, 1,
            "D1", "table1", "HD_SAAS", "ok", "cat1", 0.9, "reason", "approved",
            "ev", "2026-01-01",
        ),
    ).fetchone()
    payload = metadata_row_payload(row)
    assert payload["semanticId"] == "id1"
    assert payload["dictionaryVersion"] == "v1"
    assert payload["conceptType"] == "Device"
    assert payload["semanticKey"] == "key1"
    assert payload["canonicalName"] == "Name"
    assert payload["semanticLabelCandidate"] == "Label"
    assert payload["description"] == "Desc"
    assert payload["dataType"] == "TEXT"
    assert payload["length"] == 10
    assert payload["required"] == 1
    assert payload["domainId"] == "D1"
    assert payload["parentOrTable"] == "table1"
    assert payload["sourceSchemas"] == "HD_SAAS"
    assert payload["crossSchemaStatus"] == "ok"
    assert payload["aiCategory"] == "cat1"
    assert payload["aiConfidence"] == 0.9
    assert payload["aiReason"] == "reason"
    assert payload["semanticStatus"] == "approved"
    assert payload["evidence"] == "ev"
    assert payload["loadedAt"] == "2026-01-01"
