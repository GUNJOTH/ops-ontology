"""Tests for Knowledge Core lifecycle and release-gate invariants."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from build_business_semantics_layer import init_layer
from pipeline.knowledge_runtime import validate_knowledge_layer

ROOT = Path(__file__).resolve().parents[2]


def _published_asset(connection: sqlite3.Connection, with_source: bool = True) -> None:
    connection.row_factory = sqlite3.Row
    init_layer(connection)
    connection.execute(
        """INSERT INTO knowledge_asset(
             asset_id,asset_key,asset_type,title,canonical_definition,current_version,status,source_scope,
             knowledge_kind,package_id,knowledge_domain,lifecycle_status,created_at,updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        ("KA-1", "STD:temperature", "definition", "温度标准", "80", "v1", "published", "HD_SAAS",
         "standard", "thermal-operations", "operations", "Published", "2026-08-21T00:00:00+00:00", "2026-08-21T00:00:00+00:00"),
    )
    connection.execute(
        """INSERT INTO knowledge_asset_version(asset_version_id,asset_id,version,content_hash,definition_json,status,created_at)
           VALUES (?,?,?,?,?,?,?)""",
        ("KAV-1", "KA-1", "v1", "hash-1", json.dumps({"value": 80}), "published", "2026-08-21T00:00:00+00:00"),
    )
    connection.execute(
        """INSERT INTO knowledge_asset_binding(binding_id,asset_id,object_type,object_key,relation_type,status,evidence_json,created_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        ("KAB-1", "KA-1", "device", "HD_SAAS|SITE|A1", "appliesToObject", "accepted", "{}", "2026-08-21T00:00:00+00:00"),
    )
    if with_source:
        connection.execute(
            """INSERT INTO knowledge_asset_source(source_id,asset_id,source_kind,source_record_id,source_table,source_snapshot_id,evidence_json,created_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            ("KAS-1", "KA-1", "document", "DOC-1", "document_fragment", "snapshot-1", "{}", "2026-08-21T00:00:00+00:00"),
        )
    connection.commit()


def test_published_knowledge_requires_version_source_and_known_ontology_binding() -> None:
    connection = sqlite3.connect(":memory:")
    try:
        _published_asset(connection)
        result = validate_knowledge_layer(connection, ROOT / "standards" / "v2" / "ontology.ttl")
        assert result["status"] == "PASS", result["failures"]
        assert result["counts"]["published"] == 1
    finally:
        connection.close()


def test_published_knowledge_without_snapshot_evidence_is_blocked() -> None:
    connection = sqlite3.connect(":memory:")
    try:
        _published_asset(connection, with_source=False)
        result = validate_knowledge_layer(connection, ROOT / "standards" / "v2" / "ontology.ttl")
        assert result["status"] == "FAIL"
        assert "KNOWLEDGE_PROVENANCE_MISSING:KA-1" in result["failures"]
    finally:
        connection.close()
