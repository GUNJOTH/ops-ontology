"""Human-confirmed AI sample decisions and audit writes."""
from __future__ import annotations

import json
import sqlite3
import uuid

from fastapi import Depends, HTTPException

from app.core.auth import require_decision_auth
from app.core.db import sqlite_connection
from app.core.utils import utc_now
from app.schemas.ai_review import AiBulkDecisionRequest, AiDecisionRequest

from .common import invalidate_ai_cluster_cache, load_ai_review_sample


def save_ai_review_decision(
    request: AiDecisionRequest,
    actor: str = Depends(require_decision_auth),
) -> dict[str, object]:
    sample_id, sample_rows = load_ai_review_sample()
    if request.sample_id != sample_id:
        raise HTTPException(status_code=409, detail="AI 样本版本已变化，请刷新页面")
    row = next((item for item in sample_rows if item.get("CANDIDATE_ID") == request.candidate_id), None)
    if row is None:
        raise HTTPException(status_code=404, detail="候选不在当前 AI 样本中")
    sqlite = sqlite_connection()
    try:
        now = utc_now()
        existing = sqlite.execute("SELECT * FROM ai_review_decision WHERE idempotency_key=?", (request.idempotency_key,)).fetchone()
        if existing is not None:
            return {"sampleId": sample_id, "candidateId": existing["candidate_id"], "decision": existing["decision"], "replayed": True}
        sqlite.execute(
            "INSERT INTO ai_review_decision(decision_id,sample_id,candidate_id,decision,note,reviewer,idempotency_key,reviewed_at) VALUES (?,?,?,?,?,?,?,?)",
            (f"ai-decision-{uuid.uuid4().hex}", sample_id, request.candidate_id, request.decision, request.note.strip(), actor, request.idempotency_key, now),
        )
        sqlite.execute(
            "INSERT INTO audit_event(entity_type,entity_id,event_type,actor,payload_json,event_at) VALUES (?,?,?,?,?,?)",
            ("ai_review", request.candidate_id, "ai_sample_decision_saved", actor, json.dumps({"sample_id": sample_id, "decision": request.decision, "source_write": False, "formal_publication": False}, ensure_ascii=False), now),
        )
        sqlite.commit()
        invalidate_ai_cluster_cache()
        return {"sampleId": sample_id, "candidateId": request.candidate_id, "decision": request.decision, "replayed": False}
    except sqlite3.IntegrityError as exc:
        sqlite.rollback()
        raise HTTPException(status_code=409, detail=f"AI 复核记录冲突: {exc}") from exc
    finally:
        sqlite.close()


def save_ai_bulk_decision(
    request: AiBulkDecisionRequest,
    actor: str = Depends(require_decision_auth),
) -> dict[str, object]:
    sample_id, sample_rows = load_ai_review_sample()
    if request.sample_id != sample_id:
        raise HTTPException(status_code=409, detail="AI 样本版本已变化，请刷新页面")
    targets = [row for row in sample_rows if row.get("AI_DECISION") == ("接受候选" if request.decision == "accept_candidate" else "保留原文")]
    sqlite = sqlite_connection()
    try:
        now = utc_now()
        applied = 0
        replayed = 0
        sqlite.execute("BEGIN IMMEDIATE")
        for row in targets:
            candidate_id = row["CANDIDATE_ID"]
            idempotency_key = f"{request.idempotency_key}-{candidate_id}"
            existing = sqlite.execute("SELECT decision FROM ai_review_decision WHERE candidate_id=?", (candidate_id,)).fetchone()
            if existing is not None:
                if existing["decision"] != request.decision:
                    raise HTTPException(status_code=409, detail=f"候选 {candidate_id} 已存在不同决策")
                replayed += 1
                continue
            sqlite.execute(
                "INSERT INTO ai_review_decision(decision_id,sample_id,candidate_id,decision,note,reviewer,idempotency_key,reviewed_at) VALUES (?,?,?,?,?,?,?,?)",
                (f"ai-decision-{uuid.uuid4().hex}", sample_id, candidate_id, request.decision, "批量确认 AI 建议", actor, idempotency_key, now),
            )
            sqlite.execute(
                "INSERT INTO audit_event(entity_type,entity_id,event_type,actor,payload_json,event_at) VALUES (?,?,?,?,?,?)",
                ("ai_review", candidate_id, "ai_sample_bulk_decision_saved", actor, json.dumps({"sample_id": sample_id, "decision": request.decision, "source_write": False, "formal_publication": False}, ensure_ascii=False), now),
            )
            applied += 1
        sqlite.commit()
        invalidate_ai_cluster_cache()
        return {"sampleId": sample_id, "decision": request.decision, "targetCount": len(targets), "appliedCount": applied, "replayedCount": replayed, "sourceWrite": False, "formalPublication": False}
    except HTTPException:
        sqlite.rollback()
        raise
    except sqlite3.IntegrityError as exc:
        sqlite.rollback()
        raise HTTPException(status_code=409, detail=f"AI 批量复核记录冲突: {exc}") from exc
    finally:
        sqlite.close()
