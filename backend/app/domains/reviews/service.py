"""Review submission handler.

This module owns the single review write path previously embedded in app.main.
"""
from __future__ import annotations

import sqlite3
import sys
import uuid
from typing import Any

from app.core.config import (
    DEPENDENCY_DIR,
)

if DEPENDENCY_DIR.exists():
    sys.path.insert(0, str(DEPENDENCY_DIR))

from fastapi import Depends, HTTPException

from app.core.audit import append_audit_event
from app.core.auth import require_decision_auth
from app.core.db import sqlite_connection
from app.core.idempotency import find_audit_event_by_idempotency
from app.core.tx import begin_write, commit_write
from app.core.utils import utc_now
from app.schemas.review import ReviewRequest


def create_review(request: ReviewRequest, actor: str = Depends(require_decision_auth)) -> dict[str, Any]:
    description = (request.reviewed_description or "").strip()
    note = (request.note or "").strip()
    if request.decision in {"approved", "modified"} and not description:
        raise HTTPException(status_code=422, detail="通过或修改后通过必须填写最终统一描述")
    if request.decision in {"modified", "rejected", "deferred"} and not note:
        raise HTTPException(status_code=422, detail="该审核动作必须填写说明")

    sqlite = sqlite_connection()
    try:
        event = find_audit_event_by_idempotency(
            sqlite,
            event_type="review_submitted",
            idempotency_key=request.idempotency_key,
        )
        if event is not None:
            existing = sqlite.execute(
                "SELECT review_id,candidate_id,decision,reviewed_at,approval_receipt FROM review_decision WHERE review_id=?",
                (event["entity_id"],),
            ).fetchone()
            if existing:
                return {"reviewId": existing["review_id"], "candidateId": existing["candidate_id"], "decision": existing["decision"], "reviewState": existing["decision"], "approvalReceipt": existing["approval_receipt"], "reviewedAt": existing["reviewed_at"]}

        candidate = sqlite.execute(
            "SELECT candidate_id, batch_id, validator_status, review_state, candidate_description FROM semantic_candidate WHERE candidate_id=?",
            (request.candidate_id,),
        ).fetchone()
        if candidate is None:
            raise HTTPException(status_code=404, detail="候选记录不存在")
        if candidate["review_state"] not in {"pending", "deferred"}:
            raise HTTPException(status_code=409, detail="该候选已经完成审核，不能重复提交")
        prior_review = sqlite.execute(
            "SELECT review_id FROM review_decision WHERE candidate_id=?",
            (request.candidate_id,),
        ).fetchone()
        if prior_review is not None:
            raise HTTPException(status_code=409, detail="该候选已经存在审核凭据，不能重复提交")
        if request.decision in {"approved", "modified"} and candidate["validator_status"] != "candidate":
            raise HTTPException(status_code=409, detail="只有通过硬校验的 candidate 才能批准或修改后通过")

        now = utc_now()
        review_id = f"review-{uuid.uuid4().hex}"
        approval_receipt = f"receipt-{uuid.uuid4().hex}"
        review_state = request.decision
        begin_write(sqlite)
        sqlite.execute(
            """
            INSERT INTO review_decision(review_id,candidate_id,decision,reviewed_description,reason_code,review_note,reviewer,approval_receipt,reviewed_at)
            VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (review_id, request.candidate_id, request.decision, description or None, f"REVIEW_{request.decision.upper()}", note or None, actor, approval_receipt, now),
        )
        sqlite.execute("UPDATE semantic_candidate SET review_state=? WHERE candidate_id=?", (review_state, request.candidate_id))
        batch_id = candidate["batch_id"]
        sqlite.execute(
            """
            UPDATE batch_run SET
              needs_review_count=(SELECT count(*) FROM semantic_candidate WHERE batch_id=? AND review_state='pending'),
              approved_count=(SELECT count(*) FROM semantic_candidate WHERE batch_id=? AND review_state IN ('approved','modified'))
            WHERE batch_id=?
            """,
            (batch_id, batch_id, batch_id),
        )
        append_audit_event(
            sqlite,
            entity_type="review",
            entity_id=review_id,
            event_type="review_submitted",
            actor=actor,
            payload={"candidate_id": request.candidate_id, "decision": request.decision, "idempotency_key": request.idempotency_key},
            event_at=now,
        )
        sqlite.execute(
            "UPDATE formal_approval_queue SET status=?,note=?,updated_at=? WHERE candidate_id=?",
            (request.decision, note or "正式审批完成", now, request.candidate_id),
        )
        sqlite.execute(
            """
            UPDATE review_sample
            SET status=CASE WHEN NOT EXISTS (
              SELECT 1 FROM review_sample_item i
              JOIN semantic_candidate c ON c.candidate_id=i.candidate_id
              WHERE i.sample_id=review_sample.sample_id AND c.review_state='pending'
            ) THEN 'completed' ELSE status END
            WHERE sample_id IN (SELECT sample_id FROM review_sample_item WHERE candidate_id=?)
            """,
            (request.candidate_id,),
        )
        commit_write(sqlite)
        return {"reviewId": review_id, "candidateId": request.candidate_id, "decision": request.decision, "reviewState": review_state, "approvalReceipt": approval_receipt, "reviewedAt": now}
    except HTTPException:
        if sqlite.in_transaction:
            sqlite.rollback()
        raise
    except sqlite3.IntegrityError as exc:
        if sqlite.in_transaction:
            sqlite.rollback()
        raise HTTPException(status_code=409, detail=f"审核写入冲突：{exc}") from exc
    finally:
        sqlite.close()
