"""HTTP-facing preview and sample browsing for AI candidate review."""
from __future__ import annotations

from typing import Literal

from fastapi import Query

from app.core.db import sqlite_connection

from .ai_scope import candidate_agent_preview
from .common import CANDIDATE_AGENT_DEFAULT_BATCH_SIZE, CANDIDATE_AGENT_MAX_BATCH_SIZE, load_ai_review_sample
from .decision_policy import ai_sample_summary, auto_approval_preview
from .samples import latest_batch


def ai_review_preview(scope: Literal["sample", "batch"] = "sample") -> dict[str, object]:
    sqlite = sqlite_connection()
    try:
        return auto_approval_preview(sqlite, scope)
    finally:
        sqlite.close()


def ai_agent_review_preview(
    batch_size: int = Query(default=CANDIDATE_AGENT_DEFAULT_BATCH_SIZE, ge=10, le=CANDIDATE_AGENT_MAX_BATCH_SIZE),
) -> dict[str, object]:
    """Show the next AI-review workload without calling the model or writing state."""
    sqlite = sqlite_connection()
    try:
        batch = latest_batch(sqlite)
        return candidate_agent_preview(sqlite, batch, batch_size)
    finally:
        sqlite.close()


def ai_review_sample(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    ai_decision: Literal["all", "keep_original", "accept_candidate", "needs_review"] = "all",
    site_id: str | None = None,
) -> dict[str, object]:
    sample_id, sample_rows = load_ai_review_sample()
    sqlite = sqlite_connection()
    try:
        summary = ai_sample_summary(sqlite, sample_id, sample_rows)
        reviewed = {
            row["candidate_id"]: row
            for row in sqlite.execute(
                "SELECT candidate_id, decision, note, reviewer, reviewed_at "
                "FROM ai_review_decision WHERE sample_id=?",
                (sample_id,),
            ).fetchall()
        }
        filtered: list[dict[str, object]] = []
        for row in sample_rows:
            if site_id and row.get("SITEID") != site_id:
                continue
            existing = reviewed.get(row.get("CANDIDATE_ID", ""))
            current_decision = existing["decision"] if existing else None
            recommended_decision = {
                "保留原文": "keep_original",
                "接受候选": "accept_candidate",
                "需要复核": "needs_review",
            }.get(row.get("AI_DECISION", ""), "needs_review")
            if ai_decision != "all" and recommended_decision != ai_decision:
                continue
            filtered.append(
                {
                    "sampleId": sample_id,
                    "candidateId": row.get("CANDIDATE_ID", ""),
                    "siteId": row.get("SITEID", ""),
                    "assetNumber": row.get("ASSETNUM", ""),
                    "originalDescription": row.get("ORIGINAL_DESCRIPTION", ""),
                    "candidateDescription": row.get("EXISTING_CANDIDATE_DESCRIPTION", ""),
                    "diffCategory": row.get("DIFF_CATEGORY", ""),
                    "diffSignature": row.get("DIFF_SIGNATURE", ""),
                    "kks": row.get("LOCATION_CODE", ""),
                    "locationDescription": row.get("LOCATION_DESCRIPTION", ""),
                    "locationParent": row.get("LOCATION_PARENT", ""),
                    "classificationDescription": row.get("CLASSIFICATION_DESCRIPTION", ""),
                    "confidence": row.get("CONFIDENCE", ""),
                    "aiDecision": row.get("AI_DECISION", ""),
                    "aiConfidence": row.get("AI_CONFIDENCE", ""),
                    "aiReason": row.get("AI_REASON", ""),
                    "decision": current_decision,
                    "reviewNote": existing["note"] if existing else "",
                    "reviewer": existing["reviewer"] if existing else "",
                    "reviewedAt": existing["reviewed_at"] if existing else "",
                }
            )
        start = (page - 1) * page_size
        return {
            "rows": filtered[start : start + page_size],
            "total": len(filtered),
            "page": page,
            "pageSize": page_size,
            "summary": summary,
        }
    finally:
        sqlite.close()
