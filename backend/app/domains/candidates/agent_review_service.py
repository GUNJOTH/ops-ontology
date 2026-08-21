"""Bounded model review with deterministic local write gates."""
from __future__ import annotations

import json
import sqlite3
import uuid

from fastapi import Depends, HTTPException

from app.core.auth import require_decision_auth
from app.core.db import sqlite_connection
from app.core.utils import utc_now
from app.schemas.ai_review import AgentCandidateReviewRequest

from .ai_agent import canonicalize_pending_candidate_reviews, invoke_pending_candidate_review
from .ai_scope import candidate_agent_preview, candidate_agent_where
from .common import CANDIDATE_AGENT_REVIEW_VERSION
from .samples import latest_batch


def agent_audit_pending_candidates(
    request: AgentCandidateReviewRequest,
    actor: str = Depends(require_decision_auth),
) -> dict[str, object]:
    """Audit one bounded slice; non-approval answers remain isolated."""
    sqlite = sqlite_connection()
    try:
        existing = sqlite.execute(
            "SELECT * FROM ai_review_run WHERE idempotency_key=?",
            (request.idempotency_key,),
        ).fetchone()
        if existing is not None:
            event = sqlite.execute(
                "SELECT payload_json FROM audit_event WHERE entity_type='ai_review_run' AND entity_id=? AND event_type='candidate_agent_review_completed' ORDER BY event_id DESC LIMIT 1",
                (existing["run_id"],),
            ).fetchone()
            payload = json.loads(event["payload_json"]) if event else {}
            return {**payload, "runId": existing["run_id"], "replayed": True}

        batch = latest_batch(sqlite)
        where_sql, parameters = candidate_agent_where(batch["batch_id"], candidate_ids=request.candidate_ids)
        parameters.append(request.batch_size)
        rows = sqlite.execute(
            f"""
            SELECT candidate_id,original_description,candidate_description,semantic_action,
              confidence,evidence_level,validator_status,reason_codes_json,
              site_id,asset_number,location_code,location_description,location_parent,
              classification_description
            FROM (
              SELECT c.candidate_id,c.original_description,c.candidate_description,c.semantic_action,
                c.confidence,c.evidence_level,c.validator_status,c.reason_codes_json,
                d.site_id,d.asset_number,d.location_code,d.location_description,d.location_parent,
                d.classification_description,
                ROW_NUMBER() OVER (PARTITION BY d.site_id ORDER BY d.asset_number,c.candidate_id) AS agent_site_rank
              FROM semantic_candidate c JOIN device_identity d ON d.device_id=c.device_id
              WHERE {where_sql}
            ) eligible
            ORDER BY agent_site_rank,site_id,asset_number,candidate_id
            LIMIT ?
            """,
            parameters,
        ).fetchall()
        raw_items = invoke_pending_candidate_review(rows) if rows else []
        decisions = canonicalize_pending_candidate_reviews(raw_items, rows)
        decision_by_id = {item["candidateId"]: item for item in decisions}
        run_id = f"ai-agent-review-{uuid.uuid4().hex}"
        now = utc_now()
        approved_count = 0
        needs_review_count = 0
        rejected_count = 0
        isolated_count = 0
        skipped_count = 0
        sqlite.execute("BEGIN IMMEDIATE")
        for row in rows:
            item = decision_by_id[str(row["candidate_id"])]
            if item["decision"] == "approved":
                existing_review = sqlite.execute("SELECT review_id FROM review_decision WHERE candidate_id=?", (row["candidate_id"],)).fetchone()
                if existing_review is not None:
                    skipped_count += 1
                    continue
                review_id = f"review-{uuid.uuid4().hex}"
                note = json.dumps({"agentDecision": item["agentDecision"], "confidence": item["confidence"], "risk": item["risk"], "reason": item["reason"], "localGate": item["localGate"], "policyVersion": CANDIDATE_AGENT_REVIEW_VERSION}, ensure_ascii=False)
                sqlite.execute(
                    "INSERT INTO review_decision(review_id,candidate_id,decision,reviewed_description,reason_code,review_note,reviewer,approval_receipt,reviewed_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (review_id, row["candidate_id"], "approved", row["original_description"], "AI_AGENT_KEEP_ORIGINAL", note, "semantic-review-agent", f"receipt-{uuid.uuid4().hex}", now),
                )
                sqlite.execute("UPDATE semantic_candidate SET review_state='approved' WHERE candidate_id=?", (row["candidate_id"],))
                sqlite.execute(
                    "INSERT INTO audit_event(entity_type,entity_id,event_type,actor,payload_json,event_at) VALUES (?,?,?,?,?,?)",
                    ("review", review_id, "candidate_agent_review_approved", "semantic-review-agent", json.dumps({"candidateId": row["candidate_id"], **item, "sourceWrite": False, "formalPublication": False}, ensure_ascii=False), now),
                )
                approved_count += 1
            else:
                if item["agentDecision"] == "reject":
                    rejected_count += 1
                needs_review_count += 1
                isolated_count += 1
                sqlite.execute("UPDATE semantic_candidate SET review_state='deferred' WHERE candidate_id=? AND review_state='pending'", (row["candidate_id"],))
                sqlite.execute(
                    "INSERT INTO audit_event(entity_type,entity_id,event_type,actor,payload_json,event_at) VALUES (?,?,?,?,?,?)",
                    ("candidate", row["candidate_id"], "candidate_agent_review_isolated", "semantic-review-agent", json.dumps({"candidateId": row["candidate_id"], **item, "isolation": "deferred", "sourceWrite": False, "formalPublication": False}, ensure_ascii=False), now),
                )
        sqlite.execute(
            "UPDATE batch_run SET needs_review_count=(SELECT count(*) FROM semantic_candidate WHERE batch_id=? AND review_state='pending'), approved_count=(SELECT count(*) FROM semantic_candidate WHERE batch_id=? AND review_state IN ('approved','modified')) WHERE batch_id=?",
            (batch["batch_id"], batch["batch_id"], batch["batch_id"]),
        )
        result: dict[str, object] = {"runId": run_id, "batchId": batch["batch_id"], "policyVersion": CANDIDATE_AGENT_REVIEW_VERSION, "candidateCount": len(rows), "approvedCount": approved_count, "needsReviewCount": needs_review_count, "rejectedCount": rejected_count, "isolatedCount": isolated_count, "skippedCount": skipped_count, "status": "completed", "decisions": decisions, "sourceWrite": False, "formalPublication": False}
        sqlite.execute(
            "INSERT INTO ai_review_run(run_id,idempotency_key,batch_id,scope,policy_version,eligible_count,applied_count,skipped_count,status,started_at,finished_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, request.idempotency_key, batch["batch_id"], "batch", CANDIDATE_AGENT_REVIEW_VERSION, len(rows), approved_count, skipped_count + isolated_count, "completed", now, now),
        )
        sqlite.execute(
            "INSERT INTO audit_event(entity_type,entity_id,event_type,actor,payload_json,event_at) VALUES (?,?,?,?,?,?)",
            ("ai_review_run", run_id, "candidate_agent_review_completed", "semantic-review-agent", json.dumps({**result, "note": request.note, "operator": actor}, ensure_ascii=False), now),
        )
        sqlite.commit()
        refreshed_preview = candidate_agent_preview(sqlite, batch, request.batch_size)
        result.update({"remainingEligibleCount": refreshed_preview["eligibleCount"], "remainingPendingCount": refreshed_preview["pendingCount"], "isolatedTotalCount": refreshed_preview["isolatedCount"]})
        return result
    except HTTPException:
        if sqlite.in_transaction:
            sqlite.rollback()
        raise
    except sqlite3.IntegrityError as exc:
        if sqlite.in_transaction:
            sqlite.rollback()
        raise HTTPException(status_code=409, detail=f"candidate agent review write conflict: {exc}") from exc
    finally:
        sqlite.close()
