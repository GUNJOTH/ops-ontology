"""Regression tests for document, candidate, conflict and release gates."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from pipeline.knowledge.documents import import_document
from pipeline.knowledge.lifecycle import approve_knowledge, create_candidate, release_approved_knowledge
from pipeline.knowledge.quality import detect_conflicts, replay_knowledge


def _connection() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    return connection


def test_document_import_creates_fragments_without_publication() -> None:
    path = Path(__file__).resolve().parents[2] / "docs" / "PRODUCTIZATION_PHASE3.md"
    connection = _connection()
    result = import_document(connection, path)
    assert result["fragmentCount"] >= 1
    assert result["status"] == "draft"
    assert connection.execute("SELECT count(*) FROM knowledge_asset WHERE asset_type='fragment'").fetchone()[0] >= 1
    connection.close()


def test_conflict_blocks_knowledge_release_and_replay_is_a_report() -> None:
    connection = _connection()
    fragment = Path(__file__).resolve().parents[2] / "docs" / "PRODUCTIZATION_PHASE3.md"
    imported = import_document(connection, fragment)
    fragment_id = imported["fragmentIds"][0]
    first = create_candidate(connection, title="温度上限80", definition={"appliesTo": "Device", "predicate": "temperature", "value": 80, "condition": {"field": "temperature", "operator": ">", "value": 80}}, source_fragment_id=fragment_id)
    second = create_candidate(connection, title="温度上限85", definition={"appliesTo": "Device", "predicate": "temperature", "value": 85}, source_fragment_id=fragment_id + "-2")
    approve_knowledge(connection, first["assetId"], decision="approved", reviewer="tester", note="evidence", idempotency_key="review-1")
    approve_knowledge(connection, second["assetId"], decision="approved", reviewer="tester", note="evidence", idempotency_key="review-2")
    conflicts = detect_conflicts(connection)
    assert conflicts["conflictCount"] == 1
    release = release_approved_knowledge(connection, release_id="release-1", reviewer="tester")
    assert release["status"] == "blocked"
    replay = replay_knowledge(connection, first["assetId"], [{"caseId": "c1", "facts": {"temperature": 81}}])
    assert replay["results"][0]["result"] is True
    connection.close()


def test_evidence_backed_case_can_release_after_review() -> None:
    connection = _connection()
    source = Path(__file__).resolve().parents[2] / "docs" / "PRODUCTIZATION_PHASE3.md"
    imported = import_document(connection, source)
    candidate = create_candidate(
        connection,
        title="循环泵高温处置案例",
        knowledge_kind="case",
        definition={
            "appliesTo": "Device",
            "predicate": "bearingTemperature",
            "caseFacts": {"temperature": 91, "limit": 80},
            "outcome": "生成待审批处理建议",
        },
        source_fragment_id=imported["fragmentIds"][0],
    )
    approve_knowledge(
        connection,
        candidate["assetId"],
        decision="approved",
        reviewer="tester",
        note="来源、类绑定和案例事实已核对",
        idempotency_key="case-review-1",
    )
    release = release_approved_knowledge(connection, release_id="case-release-1", reviewer="tester")
    assert release["status"] == "released"
    assert release["assetCount"] == 1
    assert connection.execute("SELECT status FROM knowledge_asset WHERE asset_id=?", (candidate["assetId"],)).fetchone()[0] == "published"
    connection.close()
