"""Deterministic AI-gate approval service."""
from __future__ import annotations

import json
import sqlite3
import uuid

from fastapi import Depends, HTTPException

from app.core.auth import require_decision_auth
from app.core.db import sqlite_connection
from app.core.utils import utc_now
from app.schemas.ai_review import AutoApprovalRequest

from .common import AUTO_APPROVAL_POLICY_VERSION
from .decision_policy import auto_approval_filter
from .samples import ensure_review_sample, latest_batch


def ai_auto_approve(
    request: AutoApprovalRequest,
    actor: str = Depends(require_decision_auth),
) -> dict[str, object]:
    sqlite = sqlite_connection()
    try:
        existing = sqlite.execute(
            "SELECT * FROM ai_review_run WHERE idempotency_key=?",
            (request.idempotency_key,),
        ).fetchone()
        if existing is not None:
            return {
                "runId": existing["run_id"],
                "batchId": existing["batch_id"],
                "scope": existing["scope"],
                "policyVersion": existing["policy_version"],
                "eligibleCount": int(existing["eligible_count"]),
                "appliedCount": int(existing["applied_count"]),
                "skippedCount": int(existing["skipped_count"]),
                "status": existing["status"],
                "replayed": True,
            }
        batch = latest_batch(sqlite)
        sample_id = None
        if request.scope == "sample":
            sample = ensure_review_sample(sqlite, batch)
            sample_id = sample["sample_id"]
        where_sql, parameters = auto_approval_filter(batch["batch_id"], sample_id)
        eligible_rows = sqlite.execute(
            f"SELECT c.candidate_id, c.candidate_description FROM semantic_candidate c WHERE {where_sql} ORDER BY c.candidate_id",
            parameters,
        ).fetchall()
        now = utc_now()
        run_id = f"ai-review-{uuid.uuid4().hex}"
        applied = 0
        skipped = 0
        sqlite.execute("BEGIN IMMEDIATE")
        for row in eligible_rows:
            current = sqlite.execute(
                "SELECT review_state FROM semantic_candidate WHERE candidate_id=?",
                (row["candidate_id"],),
            ).fetchone()
            if current is None or current["review_state"] != "pending":
                skipped += 1
                continue
            review_id = f"review-{uuid.uuid4().hex}"
            sqlite.execute(
                "INSERT INTO review_decision (review_id,candidate_id,decision,reviewed_description,reason_code,review_note,reviewer,approval_receipt,reviewed_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (review_id, row["candidate_id"], "approved", row["candidate_description"], "AI_AUTO_APPROVE_EQUIVALENT", f"AI 辅助审批；策略 {AUTO_APPROVAL_POLICY_VERSION}；原描述与候选描述一致，未改写源数据。", "ai-gate", f"receipt-{uuid.uuid4().hex}", now),
            )
            sqlite.execute("UPDATE semantic_candidate SET review_state='approved' WHERE candidate_id=?", (row["candidate_id"],))
            sqlite.execute(
                "INSERT INTO audit_event(entity_type,entity_id,event_type,actor,payload_json,event_at) VALUES (?,?,?,?,?,?)",
                ("review", review_id, "ai_auto_approved", "ai-gate", json.dumps({"candidate_id": row["candidate_id"], "policy_version": AUTO_APPROVAL_POLICY_VERSION, "scope": request.scope, "run_id": run_id, "operator": actor}, ensure_ascii=False), now),
            )
            applied += 1
        sqlite.execute(
            "UPDATE batch_run SET needs_review_count=(SELECT count(*) FROM semantic_candidate WHERE batch_id=? AND review_state='pending'), approved_count=(SELECT count(*) FROM semantic_candidate WHERE batch_id=? AND review_state IN ('approved','modified')) WHERE batch_id=?",
            (batch["batch_id"], batch["batch_id"], batch["batch_id"]),
        )
        sqlite.execute(
            "UPDATE review_sample SET status=CASE WHEN NOT EXISTS (SELECT 1 FROM review_sample_item i JOIN semantic_candidate c ON c.candidate_id=i.candidate_id WHERE i.sample_id=review_sample.sample_id AND c.review_state='pending') THEN 'completed' ELSE status END WHERE sample_id=?",
            (sample_id,) if sample_id else ("",),
        )
        sqlite.execute(
            "INSERT INTO ai_review_run (run_id,idempotency_key,batch_id,scope,policy_version,eligible_count,applied_count,skipped_count,status,started_at,finished_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, request.idempotency_key, batch["batch_id"], request.scope, AUTO_APPROVAL_POLICY_VERSION, len(eligible_rows), applied, skipped, "completed", now, now),
        )
        sqlite.execute(
            "INSERT INTO audit_event(entity_type,entity_id,event_type,actor,payload_json,event_at) VALUES (?,?,?,?,?,?)",
            ("ai_review_run", run_id, "ai_auto_approval_completed", "ai-gate", json.dumps({"scope": request.scope, "eligible_count": len(eligible_rows), "applied_count": applied, "skipped_count": skipped, "policy_version": AUTO_APPROVAL_POLICY_VERSION, "operator": actor}, ensure_ascii=False), now),
        )
        sqlite.commit()
        return {"runId": run_id, "batchId": batch["batch_id"], "scope": request.scope, "policyVersion": AUTO_APPROVAL_POLICY_VERSION, "eligibleCount": len(eligible_rows), "appliedCount": applied, "skippedCount": skipped, "status": "completed", "replayed": False}
    except HTTPException:
        if sqlite.in_transaction:
            sqlite.rollback()
        raise
    except sqlite3.IntegrityError as exc:
        if sqlite.in_transaction:
            sqlite.rollback()
        raise HTTPException(status_code=409, detail=f"AI 辅助审批写入冲突：{exc}") from exc
    finally:
        sqlite.close()
