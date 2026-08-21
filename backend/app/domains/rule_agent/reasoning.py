"""Rule agent and semantic reasoning routes (native APIRouter)."""
from __future__ import annotations

import json
import uuid
from typing import Any

from fastapi import HTTPException

from app.core.config import (
    RULE_AGENT_BASE_URL,
    RULE_AGENT_MODEL,
)
from app.core.db import sqlite_connection
from app.core.utils import utc_now
from app.domains.candidates.service import latest_batch
from app.schemas.rule_agent import (
    SemanticReasoningRequest,
)

from .service import (
    canonicalize_semantic_reasoning,
    invoke_semantic_reasoning_agent,
    rule_agent_profile,
    semantic_reasoning_item_payload,
)


def semantic_reasoning_analyze(request: SemanticReasoningRequest) -> dict[str, Any]:
    """Reason over context clusters without creating candidates or publications."""
    sqlite = sqlite_connection()
    run_id = f"semantic-reasoning-{uuid.uuid4().hex}"
    try:
        existing = sqlite.execute(
            "SELECT reasoning_run_id FROM semantic_reasoning_run WHERE idempotency_key=?",
            (request.idempotency_key,),
        ).fetchone()
        if existing is not None:
            run = sqlite.execute(
                "SELECT * FROM semantic_reasoning_run WHERE reasoning_run_id=?",
                (existing["reasoning_run_id"],),
            ).fetchone()
            items = sqlite.execute(
                "SELECT * FROM semantic_reasoning_item WHERE reasoning_run_id=? ORDER BY reasoning_item_id",
                (existing["reasoning_run_id"],),
            ).fetchall()
            return {
                "runId": run["reasoning_run_id"],
                "status": run["status"],
                "clusterCount": int(run["cluster_count"]),
                "sampleCount": int(run["sampled_count"]),
                "items": [semantic_reasoning_item_payload(row) for row in items],
                "sourceWrite": False,
                "formalPublication": False,
            }

        batch = latest_batch(sqlite)
        profile = rule_agent_profile(sqlite, request.sample_size)
        clusters = profile.get("semanticClusters", [])
        requested_keys = {str(value).strip() for value in request.cluster_keys if str(value).strip()}
        if requested_keys:
            clusters = [item for item in clusters if item.get("clusterKey") in requested_keys]
        clusters = clusters[: request.cluster_limit]
        now = utc_now()
        sqlite.execute(
            """
            INSERT INTO semantic_reasoning_run
              (reasoning_run_id,idempotency_key,batch_id,source_snapshot_id,cluster_count,sampled_count,
               model,provider_base_url,profile_json,status,created_at,source_write,formal_publication)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                run_id,
                request.idempotency_key,
                batch["batch_id"],
                batch["source_snapshot_id"],
                len(clusters),
                int(profile.get("sampleCount", 0)),
                RULE_AGENT_MODEL,
                RULE_AGENT_BASE_URL,
                json.dumps({"profile": profile, "selectedClusters": clusters}, ensure_ascii=False),
                "running",
                now,
                0,
                0,
            ),
        )
        sqlite.commit()
        raw_items = invoke_semantic_reasoning_agent(clusters) if clusters else []
        reasoning_items = canonicalize_semantic_reasoning(raw_items, clusters)
        sqlite.execute("BEGIN IMMEDIATE")
        for index, item in enumerate(reasoning_items, start=1):
            sqlite.execute(
                """
                INSERT INTO semantic_reasoning_item
                  (reasoning_item_id,reasoning_run_id,cluster_key,decision,hypothesis,evidence_json,
                   counterexamples_json,candidate_rule_json,confidence,risk_level,required_checks_json,created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    f"{run_id}-item-{index}",
                    run_id,
                    item["clusterKey"],
                    item["decision"],
                    item["hypothesis"],
                    json.dumps(item["evidence"], ensure_ascii=False),
                    json.dumps(item["counterexamples"], ensure_ascii=False),
                    json.dumps(item["candidateRule"], ensure_ascii=False),
                    item["confidence"],
                    item["riskLevel"],
                    json.dumps(item["requiredChecks"], ensure_ascii=False),
                    now,
                ),
            )
        sqlite.execute(
            "UPDATE semantic_reasoning_run SET status='completed',finished_at=? WHERE reasoning_run_id=?",
            (utc_now(), run_id),
        )
        sqlite.execute(
            """
            INSERT INTO audit_event(entity_type,entity_id,event_type,actor,payload_json,event_at)
            VALUES (?,?,?,?,?,?)
            """,
            (
                "semantic_reasoning",
                run_id,
                "semantic_reasoning_completed",
                "semantic-reasoning-agent",
                json.dumps(
                    {
                        "runId": run_id,
                        "clusterCount": len(clusters),
                        "sampleCount": int(profile.get("sampleCount", 0)),
                        "itemCount": len(reasoning_items),
                        "sourceWrite": False,
                        "formalPublication": False,
                        "note": request.note,
                    },
                    ensure_ascii=False,
                ),
                utc_now(),
            ),
        )
        sqlite.commit()
        return {
            "runId": run_id,
            "status": "completed",
            "clusterCount": len(clusters),
            "sampleCount": int(profile.get("sampleCount", 0)),
            "items": reasoning_items,
            "sourceWrite": False,
            "formalPublication": False,
        }
    except HTTPException as exc:
        if sqlite.in_transaction:
            sqlite.rollback()
        sqlite.execute(
            "UPDATE semantic_reasoning_run SET status='failed',error_message=?,finished_at=? WHERE reasoning_run_id=?",
            (str(exc.detail), utc_now(), run_id),
        )
        sqlite.commit()
        raise
    except Exception as exc:
        if sqlite.in_transaction:
            sqlite.rollback()
        sqlite.execute(
            "UPDATE semantic_reasoning_run SET status='failed',error_message=?,finished_at=? WHERE reasoning_run_id=?",
            (type(exc).__name__, utc_now(), run_id),
        )
        sqlite.commit()
        raise HTTPException(status_code=502, detail=f"语义推理执行失败：{type(exc).__name__}") from exc
    finally:
        sqlite.close()
def semantic_reasoning_latest() -> dict[str, Any]:
    sqlite = sqlite_connection()
    try:
        run = sqlite.execute("SELECT * FROM semantic_reasoning_run ORDER BY created_at DESC LIMIT 1").fetchone()
        if run is None:
            return {"run": None, "items": [], "sourceWrite": False, "formalPublication": False}
        items = sqlite.execute(
            "SELECT * FROM semantic_reasoning_item WHERE reasoning_run_id=? ORDER BY reasoning_item_id",
            (run["reasoning_run_id"],),
        ).fetchall()
        return {
            "run": {
                "runId": run["reasoning_run_id"],
                "status": run["status"],
                "clusterCount": int(run["cluster_count"]),
                "sampleCount": int(run["sampled_count"]),
                "model": run["model"],
                "createdAt": run["created_at"],
                "finishedAt": run["finished_at"],
            },
            "items": [semantic_reasoning_item_payload(row) for row in items],
            "sourceWrite": False,
            "formalPublication": False,
        }
    finally:
        sqlite.close()
