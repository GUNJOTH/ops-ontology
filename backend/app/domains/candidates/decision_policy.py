"""Pure policy and preview helpers for AI-assisted candidate decisions."""
from __future__ import annotations

import sqlite3
from collections import Counter
from typing import Any, Literal

from .cluster_rules import AI_DECISION_KEYS
from .common import AUTO_APPROVAL_POLICY_VERSION
from .samples import ensure_review_sample, latest_batch


def ai_sample_summary(connection: sqlite3.Connection, sample_id: str, rows: list[dict[str, str]]) -> dict[str, Any]:
    decisions = {key: 0 for key in AI_DECISION_KEYS}
    for row in connection.execute(
        "SELECT decision, count(*) AS count FROM ai_review_decision WHERE sample_id=? GROUP BY decision",
        (sample_id,),
    ).fetchall():
        if row["decision"] in decisions:
            decisions[row["decision"]] = int(row["count"])
    recommended = {key: 0 for key in AI_DECISION_KEYS}
    for row in rows:
        value = row.get("AI_DECISION", "")
        if value == "保留原文":
            recommended["keep_original"] += 1
        elif value == "接受候选":
            recommended["accept_candidate"] += 1
        elif value == "需要复核":
            recommended["needs_review"] += 1
    reviewed_ids = {
        row["candidate_id"]
        for row in connection.execute(
            "SELECT candidate_id FROM ai_review_decision WHERE sample_id=?", (sample_id,)
        ).fetchall()
    }
    pending_recommendation = {key: 0 for key in AI_DECISION_KEYS}
    for row in rows:
        if row.get("CANDIDATE_ID") in reviewed_ids:
            continue
        value = row.get("AI_DECISION", "")
        if value == "保留原文":
            pending_recommendation["keep_original"] += 1
        elif value == "接受候选":
            pending_recommendation["accept_candidate"] += 1
        elif value == "需要复核":
            pending_recommendation["needs_review"] += 1
    site_counts = Counter((row.get("SITEID") or "").strip() or "未填写" for row in rows)
    classification_counts = Counter(
        (row.get("CLASSIFICATION_DESCRIPTION") or "").strip() or "未分类" for row in rows
    )
    return {
        "sampleId": sample_id,
        "total": len(rows),
        "reviewed": sum(decisions.values()),
        "pending": len(rows) - sum(decisions.values()),
        "reviewedByDecision": decisions,
        "aiRecommendation": recommended,
        "pendingByRecommendation": pending_recommendation,
        "sites": [
            {"siteId": key, "count": count}
            for key, count in sorted(site_counts.items(), key=lambda item: (-item[1], item[0]))
        ],
        "classifications": [
            {"value": key, "count": count}
            for key, count in sorted(classification_counts.items(), key=lambda item: (-item[1], item[0]))[:50]
        ],
    }


def auto_approval_filter(batch_id: str, sample_id: str | None = None) -> tuple[str, list[Any]]:
    where = [
        "c.batch_id = ?",
        "c.review_state = 'pending'",
        "c.validator_status = 'candidate'",
        "c.confidence = 'high'",
        "c.evidence_level = 'strong'",
        "length(trim(c.original_description)) > 0",
        "length(trim(c.candidate_description)) > 0",
        "trim(c.original_description) = trim(c.candidate_description)",
        "c.reason_codes_json NOT LIKE '%CONFLICT%'",
        "c.reason_codes_json NOT LIKE '%BLOCK%'",
    ]
    parameters: list[Any] = [batch_id]
    if sample_id:
        where.append(
            "EXISTS (SELECT 1 FROM review_sample_item si "
            "WHERE si.sample_id=? AND si.candidate_id=c.candidate_id)"
        )
        parameters.append(sample_id)
    return " AND ".join(where), parameters


def auto_approval_preview(connection: sqlite3.Connection, scope: Literal["sample", "batch"]) -> dict[str, Any]:
    batch = latest_batch(connection)
    sample_id = None
    selected_count = None
    if scope == "sample":
        sample = ensure_review_sample(connection, batch)
        sample_id = sample["sample_id"]
        selected_count = int(sample["selected_count"])
    where_sql, parameters = auto_approval_filter(batch["batch_id"], sample_id)
    eligible = int(
        connection.execute(
            f"SELECT count(*) FROM semantic_candidate c WHERE {where_sql}", parameters
        ).fetchone()[0]
    )
    pending_sql = "SELECT count(*) FROM semantic_candidate WHERE batch_id=? AND review_state='pending'"
    pending_parameters: list[Any] = [batch["batch_id"]]
    if sample_id:
        pending_sql = """SELECT count(*) FROM semantic_candidate c
          WHERE c.batch_id=? AND c.review_state='pending'
            AND EXISTS (SELECT 1 FROM review_sample_item si WHERE si.sample_id=? AND si.candidate_id=c.candidate_id)"""
        pending_parameters.append(sample_id)
    pending = int(connection.execute(pending_sql, pending_parameters).fetchone()[0])
    return {
        "scope": scope,
        "batchId": batch["batch_id"],
        "sampleId": sample_id,
        "sampleSelectedCount": selected_count,
        "eligibleCount": eligible,
        "pendingCount": pending,
        "policyVersion": AUTO_APPROVAL_POLICY_VERSION,
        "engine": "deterministic-ai-gate",
        "rules": [
            "仅处理待审核记录",
            "validator_status=candidate、confidence=high、evidence_level=strong",
            "原始描述与候选描述去首尾空格后完全一致",
            "排除 CONFLICT/BLOCK 原因码，源 MaxiEAM 保持只读",
        ],
    }
