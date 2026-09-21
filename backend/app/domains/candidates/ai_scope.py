"""Candidate scope and bounded workload queries for AI review."""
from __future__ import annotations

import sqlite3
from typing import Any

from app.core.config import RULE_AGENT_API_KEY, RULE_AGENT_BASE_URL, RULE_AGENT_MODEL

from .common import (
    CANDIDATE_AGENT_REVIEW_VERSION,
)


def candidate_agent_where(
    batch_id: str,
    *,
    include_previously_audited: bool = False,
    candidate_ids: list[str] | None = None,
) -> tuple[str, list[Any]]:
    """Return the local safety gate for model-assisted candidate review.

    This gate deliberately selects only high-quality, replayed candidates.  It
    never selects blocked/contradictory records and it does not let an agent
    invent a replacement description.
    """
    where = [
        "c.batch_id=?",
        "c.review_state='pending'",
        "c.publication_state='unpublished'",
        "c.validator_status='candidate'",
        "c.confidence='high'",
        "c.evidence_level IN ('strong','source_preview_and_replay')",
        "length(trim(c.original_description)) > 0",
        "length(trim(c.candidate_description)) > 0",
        "c.reason_codes_json NOT LIKE '%CONFLICT%'",
        "c.reason_codes_json NOT LIKE '%BLOCK%'",
    ]
    parameters: list[Any] = [batch_id]
    if candidate_ids:
        marks = ",".join("?" for _ in candidate_ids)
        where.append(f"c.candidate_id IN ({marks})")
        parameters.extend(candidate_ids)
    elif not include_previously_audited:
        where.append(
            "NOT EXISTS ("
            "SELECT 1 FROM audit_event ae "
            "WHERE ae.entity_type='candidate' "
            "AND ae.entity_id=c.candidate_id "
            "AND ae.event_type IN ('candidate_agent_review_isolated','candidate_agent_review_deferred')"
            ")"
        )
    return " AND ".join(where), parameters

def candidate_agent_preview(connection: sqlite3.Connection, batch: sqlite3.Row, batch_size: int) -> dict[str, Any]:
    where_sql, parameters = candidate_agent_where(batch["batch_id"])
    pending_count = int(connection.execute(
        "SELECT count(*) FROM semantic_candidate WHERE batch_id=? AND review_state='pending'",
        (batch["batch_id"],),
    ).fetchone()[0])
    eligible_count = int(connection.execute(
        f"SELECT count(*) FROM semantic_candidate c WHERE {where_sql}", parameters,
    ).fetchone()[0])
    isolated_count = max(0, pending_count - eligible_count)
    site_rows = connection.execute(
        f"""
        SELECT d.site_id, count(*) AS count
        FROM semantic_candidate c
        JOIN device_identity d ON d.device_id=c.device_id
        WHERE {where_sql}
        GROUP BY d.site_id
        ORDER BY count DESC, d.site_id
        LIMIT 12
        """,
        parameters,
    ).fetchall()
    audited_count = int(connection.execute(
        """
        SELECT count(DISTINCT ae.entity_id)
        FROM audit_event ae
        JOIN semantic_candidate c ON c.candidate_id=ae.entity_id
        WHERE c.batch_id=? AND ae.entity_type='candidate'
          AND ae.event_type IN ('candidate_agent_review_isolated','candidate_agent_review_deferred')
        """,
        (batch["batch_id"],),
    ).fetchone()[0])
    return {
        "batchId": batch["batch_id"],
        "pendingCount": pending_count,
        "eligibleCount": eligible_count,
        "nextBatchSize": min(batch_size, eligible_count),
        "isolatedCount": isolated_count,
        "auditedCount": audited_count,
        "siteCounts": [{"siteId": row["site_id"] or "未标识", "count": int(row["count"])} for row in site_rows],
        "batchSize": batch_size,
        "batchSizeOptions": [50, 100, 200, 500],
        "selectionStrategy": "round_robin_by_SITEID",
        "policyVersion": CANDIDATE_AGENT_REVIEW_VERSION,
        "model": RULE_AGENT_MODEL,
        "configured": bool(RULE_AGENT_API_KEY and RULE_AGENT_BASE_URL),
        "sourceWrite": False,
        "formalPublication": False,
        "workflow": ["高质量门禁", "AI 保守审阅", "自动通过或隔离", "人工复核隔离项", "独立审批与发布"],
        "gates": [
            "仅选择 high + candidate + 未发布记录",
            "证据为 strong 或 source_preview_and_replay",
            "排除 CONFLICT/BLOCK 原因码",
            "模型只能保留原文或进入隔离，不生成新描述",
            "不写 MaxiEAM，不进入正式发布层",
        ],
    }
