"""写路径集成测试：POST /api/reviews（写事务 + 审计 + 幂等键端到端）。

覆盖 create_review 的完整写安全栈：
find_audit_event_by_idempotency → begin_write → 多表更新 → append_audit_event →
commit_write；以及同幂等键重放的幂等返回。
"""
from __future__ import annotations

import json
import sqlite3

import pytest

pytestmark = pytest.mark.integration


NOW = "2026-08-20T00:00:00+00:00"


def _open(db_path):
    connection = sqlite3.connect(str(db_path))
    connection.row_factory = sqlite3.Row
    return connection


def _seed_review_candidate(
    connection, candidate_id: str = "cand-1", review_state: str = "pending"
) -> None:
    connection.execute(
        "INSERT INTO source_snapshot(source_snapshot_id, source_table, source_row_count, "
        "distinct_identity_count, snapshot_hash, captured_at, status) "
        "VALUES ('snap-1','device_table',1,1,'hash-snap',?, 'verified')",
        (NOW,),
    )
    connection.execute(
        "INSERT INTO batch_run(batch_id, run_id, source_snapshot_id, batch_type, rule_version, "
        "validator_version, input_count, candidate_count, status, config_json, started_at) "
        "VALUES ('batch-1','run-1','snap-1','semantic','rule-v1','val-v1',1,1,'completed','{}',?)",
        (NOW,),
    )
    connection.execute(
        "INSERT INTO device_identity(device_id, source_snapshot_id, source_schema, site_id, "
        "asset_number, source_row_hash, original_description, context_hash, analytics_row_key, created_at) "
        "VALUES (1,'snap-1','dm8','SITE1','ASSET1','hash-row','原始描述','hash-ctx','ark-1',?)",
        (NOW,),
    )
    connection.execute(
        "INSERT INTO semantic_candidate(candidate_id, batch_id, device_id, original_description, "
        "candidate_description, semantic_action, confidence, validator_status, reason_codes_json, "
        "evidence_level, applied_rule_ids_json, candidate_hash, rule_version, validator_version, "
        "review_state, publication_state, created_at) "
        "VALUES (?, 'batch-1', 1, '原始描述', '候选描述', 'replace', 'high', 'candidate', '[]', "
        "'strong', '[]', 'hash-cand', 'rule-v1', 'val-v1', ?, 'unpublished', ?)",
        (candidate_id, review_state, NOW),
    )
    connection.commit()


def test_review_submission_end_to_end(client, isolated_workflow_db) -> None:
    connection = _open(isolated_workflow_db)
    try:
        _seed_review_candidate(connection)
    finally:
        connection.close()

    body = {
        "candidateId": "cand-1",
        "decision": "approved",
        "reviewedDescription": "最终统一描述",
        "note": "审核通过",
        "idempotencyKey": "key-review-1",
    }
    response = client.post("/api/reviews", json=body)
    assert response.status_code == 200, response.text
    payload = response.json()
    review_id = payload["reviewId"]
    assert payload["candidateId"] == "cand-1"
    assert payload["decision"] == "approved"
    assert payload["reviewState"] == "approved"
    assert payload["approvalReceipt"]

    connection = _open(isolated_workflow_db)
    try:
        decision = connection.execute(
            "SELECT * FROM review_decision WHERE candidate_id='cand-1'"
        ).fetchone()
        assert decision is not None
        assert decision["decision"] == "approved"
        assert decision["reviewer"] == "local-user"

        candidate = connection.execute(
            "SELECT review_state FROM semantic_candidate WHERE candidate_id='cand-1'"
        ).fetchone()
        assert candidate["review_state"] == "approved"

        audit = connection.execute(
            "SELECT payload_json FROM audit_event WHERE event_type='review_submitted' AND entity_id=?",
            (review_id,),
        ).fetchone()
        assert audit is not None
        assert json.loads(audit["payload_json"])["idempotency_key"] == "key-review-1"
    finally:
        connection.close()

    # 幂等重放：同幂等键返回同一 reviewId，不重复写 review_decision。
    replay = client.post("/api/reviews", json=body)
    assert replay.status_code == 200
    assert replay.json()["reviewId"] == review_id

    connection = _open(isolated_workflow_db)
    try:
        count = connection.execute(
            "SELECT count(*) AS c FROM review_decision WHERE candidate_id='cand-1'"
        ).fetchone()["c"]
        assert count == 1
    finally:
        connection.close()
