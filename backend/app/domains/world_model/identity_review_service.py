"""World-model identity assertion review services."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any, Literal

from fastapi import Depends, HTTPException, Query
from pipeline.identity_review import isolate_non_device_reviews, triage_identity_reviews, write_identity_triage_report

from app.core.auth import require_decision_auth
from app.core.db import unified_semantics_connection, unified_semantics_write_connection
from app.core.utils import parse_json_array, utc_now
from app.schemas.semantic import SemanticIdentityReviewRequest, SemanticIdentityRevokeRequest


def revoke_semantic_identity(assertion_id: str, request: SemanticIdentityRevokeRequest, actor: str = Depends(require_decision_auth)) -> dict[str, Any]:
    """Revoke one local identity assertion with an auditable reason.

    Revocation changes only the local overlay lifecycle.  It deliberately does
    not change the source record, source snapshot, or canonical source key.
    """
    connection = unified_semantics_write_connection()
    try:
        assertion = connection.execute(
            "SELECT assertion_id,canonical_object_id,lifecycle_status,status,decision_mode FROM semantic_identity_assertion WHERE assertion_id=?",
            (assertion_id,),
        ).fetchone()
        if assertion is None:
            raise HTTPException(status_code=404, detail="身份断言不存在")
        existing = connection.execute(
            "SELECT audit_id,created_at,revoked_by,revocation_reason FROM semantic_identity_revocation_audit WHERE idempotency_key=?",
            (request.idempotency_key,),
        ).fetchone()
        if existing is not None:
            return {
                "status": "already_revoked",
                "assertion": dict(assertion),
                "audit": dict(existing),
                "sourceWrite": False,
                "formalPublication": False,
            }
        if assertion["lifecycle_status"] == "revoked":
            return {
                "status": "already_revoked",
                "assertion": dict(assertion),
                "audit": None,
                "sourceWrite": False,
                "formalPublication": False,
            }
        timestamp = utc_now()
        audit_id = f"SIRA-{hashlib.sha256((assertion_id + request.idempotency_key).encode('utf-8')).hexdigest()[:24]}"
        connection.execute(
            """
            INSERT INTO semantic_identity_revocation_audit(
              audit_id,assertion_id,previous_lifecycle_status,revoked_by,revocation_reason,idempotency_key,created_at
            ) VALUES (?,?,?,?,?,?,?)
            """,
            (audit_id, assertion_id, assertion["lifecycle_status"], request.reviewer.strip() or actor, request.reason.strip(), request.idempotency_key, timestamp),
        )
        connection.execute(
            """
            UPDATE semantic_identity_assertion
               SET lifecycle_status='revoked',revoked_at=?,revoked_by=?,revocation_reason=?,review_required=1,updated_at=?
             WHERE assertion_id=?
            """,
            (timestamp, request.reviewer.strip() or actor, request.reason.strip(), timestamp, assertion_id),
        )
        connection.commit()
        updated = connection.execute("SELECT * FROM semantic_identity_assertion WHERE assertion_id=?", (assertion_id,)).fetchone()
        return {
            "status": "revoked",
            "assertion": dict(updated) if updated else None,
            "audit": {"auditId": audit_id, "reviewer": request.reviewer.strip() or actor, "reason": request.reason.strip(), "createdAt": timestamp},
            "sourceWrite": False,
            "formalPublication": False,
        }
    except HTTPException:
        connection.rollback()
        raise
    except sqlite3.IntegrityError as exc:
        connection.rollback()
        raise HTTPException(status_code=409, detail=f"身份撤销审计冲突：{exc}") from exc
    finally:
        connection.close()

def semantic_identity_review_queue(
    status: Literal["all", "pending", "approved", "rejected", "blocked"] = "pending",
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    """List local identity assertions that require an explicit decision."""
    connection = unified_semantics_connection()
    try:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if "semantic_identity_review" not in tables:
            return {"schemaVersion": "semantic-identity-review-v1", "status": "not_initialized", "rows": [], "total": 0, "page": page, "pageSize": page_size, "sourceWrite": False, "formalPublication": False}
        where = ""
        parameters: list[Any] = []
        if status != "all":
            where = "WHERE r.queue_status=?"
            parameters.append(status)
        total = int(connection.execute(f"SELECT count(*) FROM semantic_identity_review r {where}", parameters).fetchone()[0])
        parameters.extend([page_size, (page - 1) * page_size])
        rows = [dict(row) for row in connection.execute(
            f"""
            SELECT r.review_id,r.assertion_id,r.queue_status,r.reviewer,r.review_note,r.evidence_json,
                   r.approval_receipt,r.created_at,r.reviewed_at,r.updated_at,
                   a.source_system,a.source_schema,a.source_table_group,a.source_table,a.source_row_id,
                   a.source_key_type,a.source_key,a.canonical_object_type,a.canonical_object_id,
                   a.assertion_type,a.status AS assertion_status,a.confidence,a.valid_from,a.valid_to,
                   a.decision_mode,a.review_required,a.lifecycle_status,a.policy_version,a.evidence_json AS assertion_evidence_json,
                   a.source_snapshot_id
              FROM semantic_identity_review r
              JOIN semantic_identity_assertion a ON a.assertion_id=r.assertion_id
              {where}
             ORDER BY CASE r.queue_status WHEN 'pending' THEN 0 WHEN 'blocked' THEN 1 ELSE 2 END,
                      r.created_at,r.review_id
             LIMIT ? OFFSET ?
            """,
            parameters,
        ).fetchall()]
        conflict_by_assertion: dict[str, list[dict[str, Any]]] = {}
        if "semantic_identity_conflict" in tables:
            for conflict in connection.execute("SELECT * FROM semantic_identity_conflict WHERE status='isolated'").fetchall():
                item = dict(conflict)
                for assertion_id in parse_json_array(conflict["assertion_ids_json"]):
                    conflict_by_assertion.setdefault(assertion_id, []).append(item)
        for row in rows:
            row["conflicts"] = conflict_by_assertion.get(str(row["assertion_id"]), [])
            row["hasConflict"] = bool(row["conflicts"])
        counts = {
            str(row["queue_status"]): int(row["count"])
            for row in connection.execute("SELECT queue_status,count(*) AS count FROM semantic_identity_review GROUP BY queue_status ORDER BY queue_status").fetchall()
        }
        return {
            "schemaVersion": "semantic-identity-review-v1",
            "status": "ready",
            "rows": rows,
            "total": total,
            "page": page,
            "pageSize": page_size,
            "counts": counts,
            "sourceWrite": False,
            "formalPublication": False,
        }
    finally:
        connection.close()


def semantic_identity_review_triage(write_report: bool = Query(default=False)) -> dict[str, Any]:
    """Return actionable categories for pending identity reviews, without deciding them."""
    connection = unified_semantics_connection()
    try:
        return write_identity_triage_report(connection) if write_report else triage_identity_reviews(connection)
    finally:
        connection.close()


def semantic_identity_review_isolate_non_device(_: str = Depends(require_decision_auth)) -> dict[str, Any]:
    """Explicitly isolate business-record keys from the device queue."""
    connection = unified_semantics_write_connection()
    try:
        return isolate_non_device_reviews(connection)
    finally:
        connection.close()

def semantic_identity_review_detail(review_id: str) -> dict[str, Any]:
    connection = unified_semantics_connection()
    try:
        row = connection.execute(
            """
            SELECT r.*,a.source_system,a.source_schema,a.source_table_group,a.source_table,a.source_row_id,
                   a.source_key_type,a.source_key,a.canonical_object_type,a.canonical_object_id,a.assertion_type,
                   a.status AS assertion_status,a.confidence,a.valid_from,a.valid_to,a.decision_mode,a.review_required,
                   a.lifecycle_status,a.policy_version,a.evidence_json AS assertion_evidence_json,a.source_snapshot_id
              FROM semantic_identity_review r
              JOIN semantic_identity_assertion a ON a.assertion_id=r.assertion_id
             WHERE r.review_id=?
            """,
            (review_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="身份审核记录不存在")
        conflicts = [dict(item) for item in connection.execute(
            "SELECT * FROM semantic_identity_conflict WHERE status='isolated' AND assertion_ids_json LIKE ? ORDER BY detected_at DESC",
            (f"%{row['assertion_id']}%",),
        ).fetchall()]
        audits = [dict(item) for item in connection.execute(
            "SELECT * FROM semantic_identity_review_audit WHERE review_id=? ORDER BY created_at DESC",
            (review_id,),
        ).fetchall()] if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='semantic_identity_review_audit'").fetchone() else []
        return {
            "schemaVersion": "semantic-identity-review-v1",
            "review": dict(row),
            "conflicts": conflicts,
            "audits": audits,
            "sourceWrite": False,
            "formalPublication": False,
        }
    finally:
        connection.close()

def decide_semantic_identity_review(review_id: str, request: SemanticIdentityReviewRequest, actor: str = Depends(require_decision_auth)) -> dict[str, Any]:
    """Record a local identity review decision; source systems remain read-only."""
    connection = unified_semantics_write_connection()
    try:
        review = connection.execute("SELECT * FROM semantic_identity_review WHERE review_id=?", (review_id,)).fetchone()
        if review is None:
            raise HTTPException(status_code=404, detail="身份审核记录不存在")
        assertion = connection.execute("SELECT * FROM semantic_identity_assertion WHERE assertion_id=?", (review["assertion_id"],)).fetchone()
        if assertion is None:
            raise HTTPException(status_code=404, detail="身份断言不存在")
        prior = connection.execute("SELECT * FROM semantic_identity_review_audit WHERE idempotency_key=?", (request.idempotency_key,)).fetchone()
        if prior is not None:
            return {"status": "already_decided", "review": dict(review), "audit": dict(prior), "sourceWrite": False, "formalPublication": False}
        if review["queue_status"] != "pending":
            return {"status": "already_decided", "review": dict(review), "audit": None, "sourceWrite": False, "formalPublication": False}
        conflicts = connection.execute(
            "SELECT conflict_id,conflict_type,source_key_type,source_key FROM semantic_identity_conflict WHERE status='isolated' AND assertion_ids_json LIKE ?",
            (f"%{review['assertion_id']}%",),
        ).fetchall()
        if request.decision == "approved" and conflicts:
            raise HTTPException(status_code=409, detail="该身份断言属于冲突组，必须先处理同组其他映射；不能单条自动解除冲突")
        timestamp = utc_now()
        receipt = f"IRV-{hashlib.sha256((review_id + request.decision + request.idempotency_key).encode('utf-8')).hexdigest()[:24]}"
        audit_id = f"SIRA-{hashlib.sha256((review_id + request.idempotency_key).encode('utf-8')).hexdigest()[:24]}"
        connection.execute(
            """
            INSERT INTO semantic_identity_review_audit(
              audit_id,review_id,assertion_id,decision,reviewer,note,evidence_json,approval_receipt,idempotency_key,created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            (audit_id, review_id, review["assertion_id"], request.decision, request.reviewer.strip() or actor, request.note.strip(), json.dumps(request.evidence, ensure_ascii=False, sort_keys=True), receipt, request.idempotency_key, timestamp),
        )
        new_assertion_status = "accepted" if request.decision == "approved" else "rejected"
        new_link_status = "accepted" if request.decision == "approved" else "blocked"
        new_relation_status = "accepted" if request.decision == "approved" else "isolated"
        connection.execute(
            """
            UPDATE semantic_identity_review
               SET queue_status=?,reviewer=?,review_note=?,evidence_json=?,approval_receipt=?,idempotency_key=?,reviewed_at=?,updated_at=?
             WHERE review_id=?
            """,
            (request.decision, request.reviewer.strip() or actor, request.note.strip(), json.dumps(request.evidence, ensure_ascii=False, sort_keys=True), receipt, request.idempotency_key, timestamp, timestamp, review_id),
        )
        connection.execute(
            """
            UPDATE semantic_identity_assertion
               SET status=?,decision_mode='manual',review_required=0,updated_at=?
             WHERE assertion_id=?
            """,
            (new_assertion_status, timestamp, review["assertion_id"]),
        )
        connection.execute(
            """
            UPDATE business_record_link
               SET status=?
             WHERE source_schema=? AND source_table=? AND source_row_id=?
            """,
            (new_link_status, assertion["source_schema"], assertion["source_table"], assertion["source_row_id"]),
        )
        if "semantic_relation_assertion" in {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}:
            connection.execute(
                """
                UPDATE semantic_relation_assertion
                   SET status=?,updated_at=?
                 WHERE source_schema=? AND source_table=? AND source_row_id=?
                """,
                (new_relation_status, timestamp, assertion["source_schema"], assertion["source_table"], assertion["source_row_id"]),
            )
        connection.commit()
        updated = connection.execute("SELECT * FROM semantic_identity_review WHERE review_id=?", (review_id,)).fetchone()
        return {
            "status": request.decision,
            "review": dict(updated) if updated else None,
            "approvalReceipt": receipt,
            "replayRequired": True,
            "sourceWrite": False,
            "formalPublication": False,
        }
    except HTTPException:
        connection.rollback()
        raise
    except sqlite3.IntegrityError as exc:
        connection.rollback()
        raise HTTPException(status_code=409, detail=f"身份审核写入冲突：{exc}") from exc
    finally:
        connection.close()
