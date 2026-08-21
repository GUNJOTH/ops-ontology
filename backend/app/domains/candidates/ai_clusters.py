"""Semantic-cluster browsing and whole-cluster decisions."""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from typing import Any, Literal

from fastapi import Depends, HTTPException, Query

from app.core.auth import require_decision_auth
from app.core.db import sqlite_connection
from app.core.utils import parse_json_array, utc_now
from app.schemas.ai_review import AiClusterDecisionRequest

from .cluster_rules import ai_cluster_id, classify_ai_cluster, cluster_pattern, summarize_ai_clusters
from .common import (
    _AI_CLUSTER_ROWS_CACHE,
    _AI_CLUSTER_SUMMARY_CACHE,
    AI_CLUSTER_CACHE_TTL_SECONDS,
    RULE_AGENT_EVIDENCE_SQL,
    invalidate_ai_cluster_cache,
)
from .samples import latest_batch


def load_ai_cluster_rows(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    """Load the unpublished changed high-quality scope without touching source tables."""
    batch = latest_batch(connection)
    batch_id = str(batch["batch_id"])

    def attach_decisions(base_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        decisions = {
            row["candidate_id"]: row["decision"]
            for row in connection.execute("SELECT candidate_id,decision FROM ai_review_decision").fetchall()
        }
        cluster_decisions = {
            row["cluster_id"]: dict(row)
            for row in connection.execute("SELECT * FROM ai_cluster_decision").fetchall()
        }
        return [
            {
                **row,
                "memberDecision": decisions.get(row["candidateId"]),
                "clusterDecision": cluster_decisions.get(row["clusterId"], {}).get("decision"),
            }
            for row in base_rows
        ]

    cached = _AI_CLUSTER_ROWS_CACHE.get(batch_id)
    if cached and time.monotonic() - cached[0] < AI_CLUSTER_CACHE_TTL_SECONDS:
        return cached[1]

    rows = connection.execute(
        f"""
        SELECT c.candidate_id,c.batch_id,c.original_description,c.candidate_description,
          c.confidence,c.validator_status,c.review_state,c.publication_state,
          c.reason_codes_json,c.evidence_level,c.applied_rule_ids_json,c.rule_version,
          c.validator_version,c.created_at,d.source_asset_id,d.site_id,d.asset_number,
          d.location_code,d.location_description,d.location_parent,
          d.classification_description
        FROM semantic_candidate c
        JOIN device_identity d ON d.device_id=c.device_id
        WHERE c.batch_id=?
          AND c.review_state='pending'
          AND c.validator_status='candidate'
          AND c.confidence='high'
          AND {RULE_AGENT_EVIDENCE_SQL}
          AND c.publication_state='unpublished'
          AND length(trim(c.original_description)) > 0
          AND length(trim(c.candidate_description)) > 0
          AND c.original_description <> c.candidate_description
        ORDER BY d.site_id,d.asset_number,c.candidate_id
        """,
        (batch_id,),
    ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        original = str(row["original_description"] or "")
        candidate = str(row["candidate_description"] or "")
        kind, label, recommendation, ai_confidence, reason = classify_ai_cluster(original, candidate)
        pattern = cluster_pattern(kind, original, candidate)
        cluster_id = ai_cluster_id(kind, pattern)
        result.append({
            "clusterId": cluster_id,
            "clusterType": kind,
            "clusterLabel": label,
            "ruleSignature": f"{kind}:{pattern}",
            "clusterPattern": pattern,
            "aiDecision": recommendation,
            "aiConfidence": ai_confidence,
            "aiReason": reason,
            "candidateId": row["candidate_id"],
            "batchId": row["batch_id"],
            "siteId": row["site_id"],
            "assetNumber": row["asset_number"],
            "originalDescription": original,
            "candidateDescription": candidate,
            "kks": row["location_code"] or "",
            "locationDescription": row["location_description"] or "",
            "locationParent": row["location_parent"] or "",
            "classificationDescription": row["classification_description"] or "",
            "confidence": row["confidence"],
            "reviewState": row["review_state"],
            "reasonCodes": parse_json_array(row["reason_codes_json"]),
            "appliedRules": parse_json_array(row["applied_rule_ids_json"]),
            "ruleVersion": row["rule_version"],
            "validatorVersion": row["validator_version"],
            "createdAt": row["created_at"],
            "memberDecision": None,
            "clusterDecision": None,
        })
    enriched = attach_decisions(result)
    _AI_CLUSTER_ROWS_CACHE[batch_id] = (time.monotonic(), enriched)
    return enriched


def ai_review_clusters(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=200),
    cluster_type: Literal["all", "question_context", "leading_minus", "terminal_hyphen", "other"] = "all",
    site_id: str | None = None,
    ai_decision: Literal["all", "accept_candidate", "keep_original", "needs_review"] = "all",
    decision: Literal["all", "pending", "accept_candidate", "keep_original", "needs_review"] = "all",
) -> dict[str, Any]:
    sqlite = sqlite_connection()
    try:
        batch_id = str(latest_batch(sqlite)["batch_id"])
        cached_summary = _AI_CLUSTER_SUMMARY_CACHE.get(batch_id)
        if cached_summary and time.monotonic() - cached_summary[0] < AI_CLUSTER_CACHE_TTL_SECONDS:
            clusters, summary = cached_summary[1], cached_summary[2]
        else:
            rows = load_ai_cluster_rows(sqlite)
            clusters, summary = summarize_ai_clusters(rows)
            _AI_CLUSTER_SUMMARY_CACHE[batch_id] = (time.monotonic(), clusters, summary)
        if cluster_type != "all":
            clusters = [item for item in clusters if item["clusterType"] == cluster_type]
        if site_id:
            clusters = [item for item in clusters if site_id in item["siteIds"]]
        if ai_decision != "all":
            clusters = [item for item in clusters if item["aiDecision"] == ai_decision]
        if decision == "pending":
            clusters = [item for item in clusters if item["decision"] is None]
        elif decision != "all":
            clusters = [item for item in clusters if item["decision"] == decision]
        start = (page - 1) * page_size
        return {
            "rows": clusters[start:start + page_size],
            "total": len(clusters),
            "page": page,
            "pageSize": page_size,
            "summary": summary,
            "filters": {"clusterType": cluster_type, "siteId": site_id or "", "aiDecision": ai_decision, "decision": decision},
        }
    finally:
        sqlite.close()


def ai_review_cluster_detail(cluster_id: str, page: int = Query(default=1, ge=1), page_size: int = Query(default=50, ge=1, le=200)) -> dict[str, Any]:
    sqlite = sqlite_connection()
    try:
        rows = [row for row in load_ai_cluster_rows(sqlite) if row["clusterId"] == cluster_id]
        if not rows:
            raise HTTPException(status_code=404, detail="语义簇不存在或已不在当前待处理范围")
        clusters, _ = summarize_ai_clusters(rows)
        cluster = clusters[0]
        start = (page - 1) * page_size
        return {
            **cluster,
            "members": rows[start:start + page_size],
            "total": len(rows),
            "page": page,
            "pageSize": page_size,
        }
    finally:
        sqlite.close()


def save_ai_cluster_decision(request: AiClusterDecisionRequest, actor: str = Depends(require_decision_auth)) -> dict[str, Any]:
    sqlite = sqlite_connection()
    try:
        rows = [row for row in load_ai_cluster_rows(sqlite) if row["clusterId"] == request.cluster_id]
        if not rows:
            raise HTTPException(status_code=404, detail="语义簇不存在或已不在当前待处理范围")
        existing_by_key = sqlite.execute("SELECT * FROM ai_cluster_decision WHERE idempotency_key=?", (request.idempotency_key,)).fetchone()
        if existing_by_key is not None:
            return {
                "clusterId": request.cluster_id,
                "decision": existing_by_key["decision"],
                "memberCount": existing_by_key["member_count"],
                "appliedCount": existing_by_key["applied_count"],
                "replayedCount": existing_by_key["member_count"],
                "replayed": True,
                "sourceWrite": False,
                "formalPublication": False,
            }
        existing_cluster = sqlite.execute("SELECT * FROM ai_cluster_decision WHERE cluster_id=?", (request.cluster_id,)).fetchone()
        if existing_cluster is not None:
            if existing_cluster["decision"] != request.decision:
                raise HTTPException(status_code=409, detail="该语义簇已经记录了不同的整簇决策")
            return {
                "clusterId": request.cluster_id,
                "decision": existing_cluster["decision"],
                "memberCount": existing_cluster["member_count"],
                "appliedCount": existing_cluster["applied_count"],
                "replayedCount": existing_cluster["member_count"],
                "replayed": True,
                "sourceWrite": False,
                "formalPublication": False,
            }
        now = utc_now()
        applied = 0
        replayed = 0
        sqlite.execute("BEGIN IMMEDIATE")
        for row in rows:
            candidate_id = row["candidateId"]
            existing = sqlite.execute("SELECT decision FROM ai_review_decision WHERE candidate_id=?", (candidate_id,)).fetchone()
            if existing is not None:
                if existing["decision"] != request.decision:
                    raise HTTPException(status_code=409, detail=f"候选 {candidate_id} 已存在不同的 AI 决策")
                replayed += 1
                continue
            member_key = f"{request.idempotency_key}:{candidate_id}"
            sqlite.execute(
                "INSERT INTO ai_review_decision(decision_id,sample_id,candidate_id,decision,note,reviewer,idempotency_key,reviewed_at) VALUES (?,?,?,?,?,?,?,?)",
                (f"ai-cluster-member-{uuid.uuid4().hex}", f"cluster:{request.cluster_id}", candidate_id, request.decision, request.note.strip() or "整簇决策", actor, member_key, now),
            )
            applied += 1
        sqlite.execute(
            "INSERT INTO ai_cluster_decision(cluster_id,decision,member_count,applied_count,note,reviewer,idempotency_key,reviewed_at) VALUES (?,?,?,?,?,?,?,?)",
            (request.cluster_id, request.decision, len(rows), applied, request.note.strip(), actor, request.idempotency_key, now),
        )
        sqlite.execute(
            "INSERT INTO audit_event(entity_type,entity_id,event_type,actor,payload_json,event_at) VALUES (?,?,?,?,?,?)",
            ("ai_cluster", request.cluster_id, "ai_cluster_decision_saved", actor, json.dumps({"decision": request.decision, "member_count": len(rows), "applied_count": applied, "source_write": False, "formal_publication": False}, ensure_ascii=False), now),
        )
        sqlite.commit()
        invalidate_ai_cluster_cache()
        return {
            "clusterId": request.cluster_id,
            "decision": request.decision,
            "memberCount": len(rows),
            "appliedCount": applied,
            "replayedCount": replayed,
            "replayed": False,
            "sourceWrite": False,
            "formalPublication": False,
        }
    except HTTPException:
        sqlite.rollback()
        raise
    except sqlite3.IntegrityError as exc:
        sqlite.rollback()
        raise HTTPException(status_code=409, detail=f"语义簇决策记录冲突: {exc}") from exc
    finally:
        sqlite.close()
