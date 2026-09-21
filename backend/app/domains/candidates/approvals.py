"""Formal approval queue handlers.

This module owns the formal approval read/approve surface. Publication remains
a separate, explicitly-gated step.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any, Literal

from fastapi import Depends, HTTPException, Query

from app.core.auth import require_decision_auth
from app.core.db import sqlite_connection
from app.core.utils import utc_now
from app.schemas.review import FormalBatchApprovalRequest

from .cleaning_sync import cleaning_registry_map, sync_cleaning_runs


def formal_approval_queue(
    status: Literal["all", "pending", "approved", "modified", "rejected", "deferred"] = "pending",
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    replay_id: str | None = None,
) -> dict[str, Any]:
    sqlite = sqlite_connection()
    try:
        where_parts: list[str] = []
        parameters: list[Any] = []
        if status != "all":
            where_parts.append("q.status=?")
            parameters.append(status)
        if replay_id:
            where_parts.append("q.replay_id=?")
            parameters.append(replay_id)
        where = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""
        total = sqlite.execute(f"SELECT count(*) FROM formal_approval_queue q {where}", parameters).fetchone()[0]
        rows = sqlite.execute(
            f"""
            SELECT q.queue_id,q.candidate_id,q.cluster_id,q.replay_id,q.proposed_decision,
              q.proposed_description,q.status,q.note,q.created_at,q.updated_at,
              c.batch_id,c.original_description,c.candidate_description,c.review_state,c.publication_state,
              d.source_snapshot_id,d.site_id,d.asset_number,d.source_asset_id,d.location_code,
              d.location_description,d.location_parent,d.classification_description,
              rr.status AS replay_status,rr.evaluation_count,rr.pass_count,rr.fail_count,
              r.review_id,r.approval_receipt,r.reviewer,r.reviewed_at
            FROM formal_approval_queue q
            JOIN semantic_candidate c ON c.candidate_id=q.candidate_id
            JOIN device_identity d ON d.device_id=c.device_id
            JOIN replay_run rr ON rr.replay_id=q.replay_id
            LEFT JOIN review_decision r ON r.candidate_id=q.candidate_id
            {where}
            ORDER BY q.created_at,q.queue_id
            LIMIT ? OFFSET ?
            """,
            parameters + [page_size, (page - 1) * page_size],
        ).fetchall()
        result = []
        for row in rows:
            result.append({
                "queueId": row["queue_id"],
                "candidateId": row["candidate_id"],
                "clusterId": row["cluster_id"],
                "replayId": row["replay_id"],
                "proposedDecision": row["proposed_decision"],
                "proposedDescription": row["proposed_description"],
                "status": row["status"],
                "note": row["note"],
                "createdAt": row["created_at"],
                "updatedAt": row["updated_at"],
                "batchId": row["batch_id"],
                "siteId": row["site_id"],
                "assetNumber": row["asset_number"],
                "assetId": row["source_asset_id"] or "",
                "originalDescription": row["original_description"],
                "candidateDescription": row["candidate_description"],
                "reviewState": row["review_state"],
                "publicationState": row["publication_state"],
                "kks": row["location_code"] or "",
                "locationDescription": row["location_description"] or "",
                "locationParent": row["location_parent"] or "",
                "classificationDescription": row["classification_description"] or "",
                "replayStatus": row["replay_status"],
                "replayEvaluationCount": row["evaluation_count"],
                "replayPassCount": row["pass_count"],
                "replayFailCount": row["fail_count"],
                "reviewId": row["review_id"] or "",
                "approvalReceipt": row["approval_receipt"] or "",
                "reviewer": row["reviewer"] or "",
                "reviewedAt": row["reviewed_at"] or "",
            })
        summary_where = ""
        summary_parameters: list[Any] = []
        if replay_id:
            summary_where = " WHERE replay_id=?"
            summary_parameters.append(replay_id)
        return {"rows": result, "total": int(total), "page": page, "pageSize": page_size, "summary": {"pending": int(sqlite.execute(f"SELECT count(*) FROM formal_approval_queue{summary_where} AND status='pending'" if summary_where else "SELECT count(*) FROM formal_approval_queue WHERE status='pending'", summary_parameters).fetchone()[0]), "completed": int(sqlite.execute(f"SELECT count(*) FROM formal_approval_queue{summary_where} AND status!='pending'" if summary_where else "SELECT count(*) FROM formal_approval_queue WHERE status!='pending'", summary_parameters).fetchone()[0])}}
    finally:
        sqlite.close()


def formal_batch_approve(request: FormalBatchApprovalRequest, actor: str = Depends(require_decision_auth)) -> dict[str, Any]:
    """Approve every pending row in one replay batch, without publishing."""
    sqlite = sqlite_connection()
    try:
        prior = sqlite.execute(
            "SELECT entity_id,payload_json FROM audit_event WHERE event_type='formal_batch_approval_completed' ORDER BY event_id DESC LIMIT 200"
        ).fetchall()
        for event in prior:
            try:
                payload = json.loads(event["payload_json"])
            except json.JSONDecodeError:
                continue
            if payload.get("idempotency_key") == request.idempotency_key:
                return payload

        replay = sqlite.execute("SELECT * FROM replay_run WHERE replay_id=?", (request.replay_id,)).fetchone()
        if replay is None:
            raise HTTPException(status_code=404, detail="回放批次不存在")
        if replay["status"] != "passed" or int(replay["fail_count"]) != 0:
            raise HTTPException(status_code=409, detail="只有回放通过且无失败的批次才能批量审批")

        rows = sqlite.execute(
            """
            SELECT q.queue_id,q.candidate_id,q.proposed_description,q.status,
              c.batch_id,c.validator_status,c.review_state,c.publication_state
            FROM formal_approval_queue q
            JOIN semantic_candidate c ON c.candidate_id=q.candidate_id
            WHERE q.replay_id=? AND q.status='pending'
            ORDER BY q.queue_id
            """,
            (request.replay_id,),
        ).fetchall()
        if not rows:
            completed = sqlite.execute(
                "SELECT count(*) FROM formal_approval_queue WHERE replay_id=? AND status!='pending'",
                (request.replay_id,),
            ).fetchone()[0]
            return {
                "replayId": request.replay_id,
                "targetCount": 0,
                "appliedCount": 0,
                "pendingCount": 0,
                "completedCount": int(completed),
                "status": "already_completed",
                "idempotencyKey": request.idempotency_key,
                "sourceWrite": False,
                "formalPublication": False,
            }
        invalid = [
            row["candidate_id"]
            for row in rows
            if row["validator_status"] != "candidate"
            or row["review_state"] != "pending"
            or row["publication_state"] != "unpublished"
            or not str(row["proposed_description"] or "").strip()
        ]
        if invalid:
            raise HTTPException(status_code=409, detail=f"批次存在不满足审批门禁的记录：{len(invalid)} 条")

        now = utc_now()
        note = request.note.strip() or f"批次正式审批：回放 {replay['pass_count']}/{replay['evaluation_count']} 通过；审批后仍需独立发布。"
        sqlite.execute("BEGIN IMMEDIATE")
        receipts: list[str] = []
        for row in rows:
            review_id = f"review-{uuid.uuid4().hex}"
            receipt = f"receipt-{uuid.uuid4().hex}"
            sqlite.execute(
                """
                INSERT INTO review_decision
                  (review_id,candidate_id,decision,reviewed_description,reason_code,review_note,reviewer,approval_receipt,reviewed_at)
                VALUES (?,?,?,?,?,?,?,?,?)
                """,
                (review_id, row["candidate_id"], "modified", row["proposed_description"], "FORMAL_BATCH_APPROVED", note, actor, receipt, now),
            )
            sqlite.execute("UPDATE semantic_candidate SET review_state='modified' WHERE candidate_id=?", (row["candidate_id"],))
            sqlite.execute("UPDATE formal_approval_queue SET status='modified',note=?,updated_at=? WHERE candidate_id=?", (note, now, row["candidate_id"]))
            receipts.append(receipt)

        batch_ids = sorted({row["batch_id"] for row in rows})
        for batch_id in batch_ids:
            sqlite.execute(
                """
                UPDATE batch_run SET
                  needs_review_count=(SELECT count(*) FROM semantic_candidate WHERE batch_id=? AND review_state='pending'),
                  approved_count=(SELECT count(*) FROM semantic_candidate WHERE batch_id=? AND review_state IN ('approved','modified'))
                WHERE batch_id=?
                """,
                (batch_id, batch_id, batch_id),
            )
        # Keep the generic cleaning task state aligned with the formal queue
        # after approval.  Without this, the queue is complete but publication
        # still sees the task as pending approval.
        sync_cleaning_runs(sqlite, cleaning_registry_map(sqlite), now)
        payload = {
            "replayId": request.replay_id,
            "targetCount": len(rows),
            "appliedCount": len(rows),
            "pendingCount": 0,
            "completedCount": len(rows),
            "status": "completed",
            "idempotencyKey": request.idempotency_key,
            "approvalReceiptCount": len(receipts),
            "sourceWrite": False,
            "formalPublication": False,
        }
        sqlite.execute(
            "INSERT INTO audit_event(entity_type,entity_id,event_type,actor,payload_json,event_at) VALUES (?,?,?,?,?,?)",
            (
                "formal_approval_queue", f"batch-{request.replay_id}", "formal_batch_approval_completed", actor,
                json.dumps(payload, ensure_ascii=False), now,
            ),
        )
        sqlite.commit()
        return payload
    except HTTPException:
        if sqlite.in_transaction:
            sqlite.rollback()
        raise
    except sqlite3.IntegrityError as exc:
        if sqlite.in_transaction:
            sqlite.rollback()
        raise HTTPException(status_code=409, detail=f"批量审批写入冲突：{exc}") from exc
    finally:
        sqlite.close()
