"""Candidate/review shared helpers.

These helpers are used by candidate, AI review, dashboard, published and review
routes.  Keeping them here lets domain routers avoid importing ``app.main``.
"""
from __future__ import annotations

import csv
import hashlib
import json
import re
import sqlite3
import time
import uuid
from collections import Counter
from typing import Any, Literal

from fastapi import Depends, HTTPException, Query

from app.agent.client import (
    RuleAgentCallError,
    invoke_rule_agent_completion,
    parse_rule_agent_json,
    rule_agent_failure_detail,
)
from app.core.auth import require_decision_auth
from app.core.config import (
    AI_REVIEW_MANIFEST,
    AI_REVIEW_SAMPLE_CSV,
    RULE_AGENT_API_KEY,
    RULE_AGENT_BASE_URL,
    RULE_AGENT_MODEL,
)
from app.core.db import sqlite_connection
from app.core.utils import parse_json_array, utc_now
from app.schemas.ai_review import (
    AgentCandidateReviewRequest,
    AiBulkDecisionRequest,
    AiClusterDecisionRequest,
    AiDecisionRequest,
    AutoApprovalRequest,
)
from app.schemas.review import FormalBatchApprovalRequest

DEFAULT_SAMPLE_TARGET = 300
MAX_SAMPLE_TARGET = 5000
CANDIDATE_AGENT_MAX_BATCH_SIZE = 500
CANDIDATE_AGENT_EVIDENCE_LEVELS = ("strong", "source_preview_and_replay")
RULE_AGENT_EVIDENCE_SQL = "c.evidence_level IN ('strong', 'source_preview_and_replay')"
AI_CLUSTER_CACHE_TTL_SECONDS = 300
_AI_CLUSTER_ROWS_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_AI_CLUSTER_SUMMARY_CACHE: dict[str, tuple[float, list[dict[str, Any]], dict[str, Any]]] = {}

def cleaning_registry(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    return connection.execute(
        "SELECT rule_key,cleaning_type,rule_label,action_label,is_cleaning,replay_id,rule_version,enabled FROM cleaning_rule_registry WHERE enabled=1 ORDER BY is_cleaning DESC,rule_key"
    ).fetchall()


def cleaning_registry_map(connection: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    return {row["replay_id"]: dict(row) for row in cleaning_registry(connection)}


def sync_cleaning_runs(connection: sqlite3.Connection, registry: dict[str, dict[str, Any]], now: str | None = None) -> None:
    now = now or utc_now()
    if not registry:
        return
    replay_ids = tuple(registry)
    marks = ",".join("?" for _ in replay_ids)
    counts_by_replay = {
        row["replay_id"]: row
        for row in connection.execute(
            f"""
            SELECT q.replay_id,
              COUNT(*) total,
              SUM(CASE WHEN q.status='pending' THEN 1 ELSE 0 END) pending,
              SUM(CASE WHEN q.status!='pending' THEN 1 ELSE 0 END) approved,
              SUM(CASE WHEN c.publication_state='published' THEN 1 ELSE 0 END) published,
              MIN(c.batch_id) batch_id
            FROM formal_approval_queue q
            JOIN semantic_candidate c ON c.candidate_id=q.candidate_id
            WHERE q.replay_id IN ({marks})
            GROUP BY q.replay_id
            """,
            replay_ids,
        ).fetchall()
    }
    preview_by_replay = {
        row["replay_id"]: row
        for row in connection.execute(
            f"SELECT replay_id,preview_rows FROM cleaning_run WHERE replay_id IN ({marks})",
            replay_ids,
        ).fetchall()
    }
    replay_by_id = {
        row["replay_id"]: row
        for row in connection.execute(
            f"SELECT replay_id,status,fail_count FROM replay_run WHERE replay_id IN ({marks})",
            replay_ids,
        ).fetchall()
    }
    existing_by_replay = {
        row["replay_id"]: row
        for row in connection.execute(
            f"SELECT replay_id,status,stage,source_type FROM cleaning_run WHERE replay_id IN ({marks})",
            replay_ids,
        ).fetchall()
    }
    for replay_id, meta in registry.items():
        counts = counts_by_replay.get(replay_id)
        if counts is None:
            counts = {"total": 0, "pending": 0, "approved": 0, "published": 0, "batch_id": None}
        pending = int(counts["pending"] or 0)
        approved = int(counts["approved"] or 0)
        published = int(counts["published"] or 0)
        total = int(counts["total"] or 0)
        task_row = preview_by_replay.get(replay_id)
        preview_rows = int(task_row["preview_rows"] or 0) if task_row else 0
        replay = replay_by_id.get(replay_id)
        replay_status = replay["status"] if replay else None
        replay_fail_count = int(replay["fail_count"] or 0) if replay else 1
        existing_task = existing_by_replay.get(replay_id)
        is_approved_agent_task = bool(
            existing_task
            and existing_task["source_type"] == "rule_agent"
            and existing_task["stage"] in {"approved", "published"}
            and replay_status == "passed"
            and replay_fail_count == 0
        )
        status = "published" if total and published == total else "approved" if approved and pending == 0 else "pending_approval" if total else (existing_task["status"] if is_approved_agent_task else "draft")
        stage = (
            "published" if total and published == total else
            "approved" if approved and pending == 0 else
            "replayed" if replay_status == "passed" and replay_fail_count == 0 else
            "previewed" if total else
            (existing_task["stage"] if is_approved_agent_task else "task")
        )
        connection.execute(
            """
            UPDATE cleaning_run
            SET batch_id=?,status=?,stage=?,candidate_count=?,pending_count=?,approved_count=?,published_count=?,
                formal_publication=?,source_write=0,updated_at=?
            WHERE replay_id=?
            """,
            (
                counts["batch_id"],
                status,
                stage,
                total or preview_rows,
                pending,
                approved,
                published,
                1 if total and published == total else 0,
                now,
                replay_id,
            ),
        )



def latest_batch(connection: sqlite3.Connection) -> sqlite3.Row:
    row = connection.execute("SELECT * FROM batch_run ORDER BY started_at DESC LIMIT 1").fetchone()
    if row is None:
        raise HTTPException(status_code=503, detail="SQLite 中没有可用批次")
    return row



def ensure_review_sample(connection: sqlite3.Connection, batch: sqlite3.Row, target_count: int = DEFAULT_SAMPLE_TARGET) -> sqlite3.Row:
    """Create one deterministic, stratified sample for the latest batch."""
    if target_count < 1:
        raise HTTPException(status_code=400, detail="sample_size must be positive")
    sample_name = f"high-quality-{target_count}-v1"
    existing = connection.execute(
        "SELECT * FROM review_sample WHERE batch_id=? AND sample_name=?",
        (batch["batch_id"], sample_name),
    ).fetchone()
    if existing is not None:
        return existing

    rows = connection.execute(
        """
        SELECT c.candidate_id, d.site_id,
          COALESCE(NULLIF(trim(d.classification_description), ''), '未分类') AS classification,
          d.asset_number
        FROM semantic_candidate c
        JOIN device_identity d ON d.device_id=c.device_id
        WHERE c.batch_id=? AND c.validator_status='candidate' AND c.review_state='pending'
        ORDER BY d.site_id, classification, d.asset_number, c.candidate_id
        """,
        (batch["batch_id"],),
    ).fetchall()
    strata: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        key = f"{row['site_id']} / {row['classification']}"
        strata.setdefault(key, []).append(row)

    selected: list[tuple[sqlite3.Row, str]] = []
    ordered_strata = sorted(strata)
    while len(selected) < target_count:
        progressed = False
        for stratum in ordered_strata:
            bucket = strata[stratum]
            if bucket:
                selected.append((bucket.pop(0), stratum))
                progressed = True
                if len(selected) == target_count:
                    break
        if not progressed:
            break

    now = utc_now()
    sample_id = f"sample-{batch['batch_id']}-{sample_name}"
    strategy = "round_robin_by_SITEID_and_CLASSIFICATION_DESCRIPTION"
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            INSERT INTO review_sample
              (sample_id,batch_id,source_snapshot_id,sample_name,target_count,selected_count,strategy,status,rule_version,validator_version,created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            (sample_id, batch["batch_id"], batch["source_snapshot_id"], sample_name, target_count, len(selected), strategy, "open", batch["rule_version"], batch["validator_version"], now),
        )
        connection.executemany(
            "INSERT INTO review_sample_item(sample_id,candidate_id,ordinal,stratum,selected_at) VALUES (?,?,?,?,?)",
            [(sample_id, row["candidate_id"], ordinal, stratum, now) for ordinal, (row, stratum) in enumerate(selected, start=1)],
        )
        connection.execute(
            "INSERT INTO audit_event(entity_type,entity_id,event_type,actor,payload_json,event_at) VALUES (?,?,?,?,?,?)",
            ("review_sample", sample_id, "review_sample_created", "semantic-api", json.dumps({"target_count": target_count, "selected_count": len(selected), "strategy": strategy}, ensure_ascii=False), now),
        )
        connection.commit()
    except sqlite3.IntegrityError:
        if connection.in_transaction:
            connection.rollback()
        existing = connection.execute(
            "SELECT * FROM review_sample WHERE batch_id=? AND sample_name=?",
            (batch["batch_id"], sample_name),
        ).fetchone()
        if existing is None:
            raise
        return existing
    return connection.execute("SELECT * FROM review_sample WHERE sample_id=?", (sample_id,)).fetchone()



def review_sample_summary(connection: sqlite3.Connection, sample: sqlite3.Row) -> dict[str, Any]:
    counts = connection.execute(
        """
        SELECT c.review_state, count(*) AS count
        FROM review_sample_item i
        JOIN semantic_candidate c ON c.candidate_id=i.candidate_id
        WHERE i.sample_id=?
        GROUP BY c.review_state
        """,
        (sample["sample_id"],),
    ).fetchall()
    summary = {"pending": 0, "approved": 0, "modified": 0, "rejected": 0, "deferred": 0}
    for row in counts:
        summary[row["review_state"]] = int(row["count"])
    strata = connection.execute(
        "SELECT stratum, count(*) AS count FROM review_sample_item WHERE sample_id=? GROUP BY stratum ORDER BY stratum",
        (sample["sample_id"],),
    ).fetchall()
    return {
        "sampleId": sample["sample_id"],
        "sampleName": sample["sample_name"],
        "batchId": sample["batch_id"],
        "sourceSnapshotId": sample["source_snapshot_id"],
        "targetCount": int(sample["target_count"]),
        "selectedCount": int(sample["selected_count"]),
        "status": "completed" if summary["pending"] == 0 and sample["selected_count"] else sample["status"],
        "strategy": sample["strategy"],
        "ruleVersion": sample["rule_version"],
        "validatorVersion": sample["validator_version"],
        "pendingCount": summary["pending"],
        "approvedCount": summary["approved"],
        "modifiedCount": summary["modified"],
        "rejectedCount": summary["rejected"],
        "deferredCount": summary["deferred"],
        "strata": [{"stratum": row["stratum"], "count": int(row["count"])} for row in strata],
    }



def row_to_candidate(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "candidateId": row["candidate_id"],
        "batchId": row["batch_id"],
        "siteId": row["site_id"],
        "assetNumber": row["asset_number"],
        "originalDescription": row["original_description"],
        "candidateDescription": row["candidate_description"],
        "kks": row["location_code"] or "",
        "locationDescription": row["location_description"] or "",
        "locationParent": row["location_parent"] or "",
        "classificationDescription": row["classification_description"] or "",
        "confidence": row["confidence"],
        "validatorStatus": row["validator_status"],
        "reviewState": row["review_state"],
        "reasonCodes": parse_json_array(row["reason_codes_json"]),
        "evidenceLevel": row["evidence_level"],
        "updatedAt": row["created_at"],
    }



def row_to_publication(row: sqlite3.Row) -> dict[str, Any]:
    """Map the formal-result row without exposing mutable source-table state."""
    applied_rules = parse_json_array(row["applied_rule_ids_json"])
    original = row["original_description"] or ""
    final = row["final_description"] or ""
    if "format.fullwidth_parenthesis_to_ascii" not in applied_rules and final != original and any(mark in original for mark in ("（", "）")):
        applied_rules.append("format.fullwidth_parenthesis_to_ascii")
    if "format.fullwidth_comma_to_ascii" not in applied_rules and final != original and "，" in original:
        applied_rules.append("format.fullwidth_comma_to_ascii")
    return {
        "publicationId": row["publication_id"],
        "candidateId": row["candidate_id"],
        "reviewId": row["review_id"],
        "batchId": row["batch_id"],
        "sourceSnapshotId": row["source_snapshot_id"],
        "siteId": row["site_id"],
        "assetNumber": row["asset_number"],
        "assetId": row["source_asset_id"] or "",
        "originalDescription": row["original_description"] or "",
        "finalDescription": row["final_description"],
        "kks": row["location_code"] or "",
        "locationDescription": row["location_description"] or "",
        "locationParent": row["location_parent"] or "",
        "classificationDescription": row["classification_description"] or "",
        "appliedRules": applied_rules,
        "ruleVersion": row["rule_version"],
        "validatorVersion": row["validator_version"],
        "replayId": row["replay_id"],
        "publishedBy": row["published_by"],
        "publishedAt": row["published_at"],
        "approvalReceipt": row["approval_receipt"],
        "reviewer": row["reviewer"],
        "reviewedAt": row["reviewed_at"],
    }



PUBLISHED_SELECT = """
    SELECT p.publication_id,p.candidate_id,p.review_id,p.source_snapshot_id,
      p.site_id,p.asset_number,p.final_description,p.rule_version,
      p.validator_version,p.replay_id,p.published_by,p.published_at,
      c.batch_id,c.original_description,c.applied_rule_ids_json,
      d.source_asset_id,d.location_code,d.location_description,d.location_parent,
      d.classification_description,r.approval_receipt,r.reviewer,r.reviewed_at
    FROM published_description p
    JOIN semantic_candidate c ON c.candidate_id=p.candidate_id
    JOIN device_identity d ON d.device_id=c.device_id
    JOIN review_decision r ON r.review_id=p.review_id
"""


CANDIDATE_AGENT_REVIEW_VERSION = "candidate-keep-original-agent-20260817-v2"
CANDIDATE_AGENT_DEFAULT_BATCH_SIZE = 100


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


def invoke_pending_candidate_review(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    """Ask the model to audit pending candidates; it cannot write or publish."""
    records: list[dict[str, Any]] = []
    for row in rows:
        try:
            reason_codes = json.loads(row["reason_codes_json"] or "[]")
        except json.JSONDecodeError:
            reason_codes = []
        records.append(
            {
                "candidateId": row["candidate_id"],
                "siteId": row["site_id"] or "",
                "assetNumber": row["asset_number"] or "",
                "originalDescription": row["original_description"] or "",
                "candidateDescription": row["candidate_description"] or "",
                "semanticAction": row["semantic_action"] or "",
                "confidence": row["confidence"] or "",
                "evidenceLevel": row["evidence_level"] or "",
                "validatorStatus": row["validator_status"] or "",
                "reasonCodes": reason_codes,
                "kks": row["location_code"] or "",
                "locationDescription": row["location_description"] or "",
                "locationParent": row["location_parent"] or "",
                "classificationDescription": row["classification_description"] or "",
            }
        )
    payload = {
        "task": "Audit pending power-plant equipment descriptions. Preserve the original description when it is the safest supported outcome.",
        "outputSchema": {
            "reviews": [
                {
                    "candidateId": "",
                    "decision": "approve_keep_original|needs_review|reject",
                    "confidence": 0.0,
                    "reason": "",
                    "risk": "low|medium|high",
                }
            ]
        },
        "policy": [
            "Use only the supplied original description and explicit context fields.",
            "Never invent manufacturer, model, capacity, equipment type, KKS, location, or classification.",
            "approve_keep_original only when the original is safe to retain, there is no conflict or block reason, and no candidate rewrite is justified.",
            "A leading minus may be a meaningful negative elevation or voltage sign; do not remove it.",
            "Return needs_review when evidence is basic, context is insufficient, or any semantic conflict is present.",
            "Return JSON only. Do not propose a replacement description.",
        ],
        "records": records,
    }
    try:
        text = invoke_rule_agent_completion(
            [
                {"role": "system", "content": "You are a conservative equipment-description audit agent. Return valid JSON only. Never modify source data."},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            "设备候选审核智能体",
        )
        parsed = parse_rule_agent_json(text, "设备候选审核智能体")
    except RuleAgentCallError as exc:
        status = 503 if exc.code == "agent_not_configured" else 502
        raise HTTPException(status_code=status, detail=rule_agent_failure_detail(exc)) from exc
    reviews = parsed.get("reviews", []) if isinstance(parsed, dict) else parsed
    return [item for item in reviews if isinstance(item, dict)] if isinstance(reviews, list) else []


def canonicalize_pending_candidate_reviews(
    raw_items: list[dict[str, Any]], rows: list[sqlite3.Row]
) -> list[dict[str, Any]]:
    allowed = {str(row["candidate_id"]): row for row in rows}
    by_candidate: dict[str, dict[str, Any]] = {}
    aliases = {
        "approve_keep_original": "approve_keep_original",
        "keep_original": "approve_keep_original",
        "approve": "approve_keep_original",
        "approved": "approve_keep_original",
        "pass": "approve_keep_original",
        "needs_review": "needs_review",
        "review": "needs_review",
        "reject": "reject",
        "rejected": "reject",
    }
    for item in raw_items:
        candidate_id = str(item.get("candidateId") or item.get("candidate_id") or "").strip()
        if candidate_id not in allowed or candidate_id in by_candidate:
            continue
        decision = aliases.get(str(item.get("decision") or "needs_review").strip().lower(), "needs_review")
        try:
            confidence = max(0.0, min(1.0, float(item.get("confidence") or 0)))
        except (TypeError, ValueError):
            confidence = 0.0
        by_candidate[candidate_id] = {
            "candidateId": candidate_id,
            "agentDecision": decision,
            "confidence": confidence,
            "reason": str(item.get("reason") or "agent did not provide a reason").strip()[:2000],
            "risk": str(item.get("risk") or "medium").strip().lower(),
        }
    results: list[dict[str, Any]] = []
    for row in rows:
        candidate_id = str(row["candidate_id"])
        item = by_candidate.get(
            candidate_id,
            {"candidateId": candidate_id, "agentDecision": "needs_review", "confidence": 0.0, "reason": "agent returned no decision", "risk": "high"},
        )
        reason_codes = []
        try:
            reason_codes = json.loads(row["reason_codes_json"] or "[]")
        except json.JSONDecodeError:
            pass
        blocking_reason = any("CONFLICT" in str(code).upper() or "BLOCK" in str(code).upper() for code in reason_codes)
        original = row["original_description"] or ""
        candidate = row["candidate_description"] or ""
        local_safe = (
            row["validator_status"] == "candidate"
            and row["confidence"] == "high"
            and row["evidence_level"] in CANDIDATE_AGENT_EVIDENCE_LEVELS
            and not blocking_reason
            and bool(original.strip())
            and bool(candidate.strip())
            and (
                "replay_passed" in reason_codes
                and "source_identity_verified" in reason_codes
            )
        )
        approved = item["agentDecision"] == "approve_keep_original" and item["confidence"] >= 0.85 and local_safe
        item["decision"] = "approved" if approved else "needs_review"
        item["localGate"] = "passed" if local_safe else "blocked"
        results.append(item)
    return results


AI_SAMPLE_ID = "next-ai-review-20260812T085842Z"
AI_DECISION_KEYS = ("keep_original", "accept_candidate", "needs_review")


AI_DECISION_KEYS = ("keep_original", "accept_candidate", "needs_review")



def load_ai_review_sample() -> tuple[str, list[dict[str, str]]]:
    if not AI_REVIEW_SAMPLE_CSV.exists():
        raise HTTPException(status_code=503, detail="AI 语义样本尚未生成")
    sample_id = AI_SAMPLE_ID
    if AI_REVIEW_MANIFEST.exists():
        try:
            sample_id = str(json.loads(AI_REVIEW_MANIFEST.read_text(encoding="utf-8")).get("sample_id") or AI_SAMPLE_ID)
        except (OSError, json.JSONDecodeError):
            pass
    with AI_REVIEW_SAMPLE_CSV.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    return sample_id, rows


def cluster_pattern(kind: str, original: str, candidate: str) -> str:
    """Return a stable, explainable rule signature for the changed description."""
    value = original
    if kind == "leading_minus" and value.startswith("-"):
        value = value[1:]
    if kind == "terminal_hyphen" and value.endswith("-"):
        value = value[:-1]
    value = value.replace("？", "?")
    value = re.sub(r"\d+(?:\.\d+)?", "{N}", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value or "<empty>"


def classify_ai_cluster(original: str, candidate: str) -> tuple[str, str, str, float, str]:
    """Classify only the current 542 changed high-quality rows.

    The classifier is deliberately conservative: a cluster decision is a local
    review record and never changes semantic_candidate.review_state.
    """
    if original.replace("？", "?") == candidate and original != candidate:
        return (
            "question_context",
            "问号上下文",
            "needs_review",
            0.88,
            "问号可能表达相位、占位或未知字段，先按簇确认，不自动删除。",
        )
    if original.startswith("-") and original[1:] == candidate:
        return (
            "leading_minus",
            "前导负号",
            "keep_original",
            0.99,
            "删除前导负号可能改变负标高、负电压或负值含义，默认保留原文。",
        )
    if original.endswith("-") and original[:-1] == candidate:
        return (
            "terminal_hyphen",
            "末尾横线",
            "needs_review",
            0.83,
            "末尾横线可能是孤立标记，也可能属于设备命名习惯，需按簇确认。",
        )
    return (
        "other",
        "其他差异",
        "needs_review",
        0.60,
        "差异未命中当前确定性规则，保留人工复核入口。",
    )


def ai_cluster_id(kind: str, pattern: str) -> str:
    digest = hashlib.sha256(f"remaining-diff-v1|{kind}|{pattern}".encode("utf-8")).hexdigest()[:20]
    return f"cluster-{digest}"


def invalidate_ai_cluster_cache() -> None:
    _AI_CLUSTER_ROWS_CACHE.clear()
    _AI_CLUSTER_SUMMARY_CACHE.clear()


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


def summarize_ai_clusters(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(row["clusterId"], []).append(row)
    clusters: list[dict[str, Any]] = []
    suggestion_counts = {key: 0 for key in AI_DECISION_KEYS}
    decision_counts = {key: 0 for key in AI_DECISION_KEYS}
    reviewed_candidates = 0
    site_counts = Counter((row.get("siteId") or "").strip() or "未填写" for row in rows)
    classification_counts = Counter((row.get("classificationDescription") or "").strip() or "未分类" for row in rows)
    for cluster_id, members in grouped.items():
        members.sort(key=lambda item: (item["siteId"], item["assetNumber"], item["candidateId"]))
        first = members[0]
        # A sample-row decision is evidence on a member, not an implicit
        # approval of the whole cluster. Only the explicit cluster table entry
        # can move a cluster out of the pending state.
        cluster_decision = first["clusterDecision"]
        reviewed_count = sum(1 for item in members if item["memberDecision"])
        reviewed_candidates += reviewed_count
        suggestion_counts[first["aiDecision"]] += 1
        if cluster_decision in decision_counts:
            decision_counts[cluster_decision] += 1
        sites: dict[str, int] = {}
        for item in members:
            sites[item["siteId"]] = sites.get(item["siteId"], 0) + 1
        clusters.append({
            "clusterId": cluster_id,
            "clusterType": first["clusterType"],
            "clusterLabel": first["clusterLabel"],
            "clusterPattern": first["clusterPattern"],
            "ruleSignature": first["ruleSignature"],
            "memberCount": len(members),
            "reviewedCount": reviewed_count,
            "pendingCount": len(members) - reviewed_count,
            "siteIds": sorted(sites),
            "sites": [{"siteId": site_id, "count": count} for site_id, count in sorted(sites.items())],
            "aiDecision": first["aiDecision"],
            "aiConfidence": first["aiConfidence"],
            "aiReason": first["aiReason"],
            "decision": cluster_decision,
            "sample": {key: first[key] for key in ("candidateId", "siteId", "assetNumber", "originalDescription", "candidateDescription", "kks", "locationDescription", "locationParent", "classificationDescription")},
        })
    clusters.sort(key=lambda item: (-item["memberCount"], item["clusterType"], item["clusterId"]))
    summary = {
        "candidateCount": len(rows),
        "clusterCount": len(clusters),
        "pendingCandidateCount": len(rows) - reviewed_candidates,
        "reviewedCandidateCount": reviewed_candidates,
        "pendingClusterCount": sum(1 for item in clusters if item["decision"] is None),
        "aiRecommendation": suggestion_counts,
        "clusterDecision": decision_counts,
        "sites": [{"siteId": key, "count": count} for key, count in sorted(site_counts.items(), key=lambda item: (-item[1], item[0]))],
        "classifications": [{"value": key, "count": count} for key, count in sorted(classification_counts.items(), key=lambda item: (-item[1], item[0]))[:50]],
    }
    return clusters, summary


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
    reviewed_ids = {row["candidate_id"] for row in connection.execute("SELECT candidate_id FROM ai_review_decision WHERE sample_id=?", (sample_id,)).fetchall()}
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
    classification_counts = Counter((row.get("CLASSIFICATION_DESCRIPTION") or "").strip() or "未分类" for row in rows)
    return {
        "sampleId": sample_id,
        "total": len(rows),
        "reviewed": sum(decisions.values()),
        "pending": len(rows) - sum(decisions.values()),
        "reviewedByDecision": decisions,
        "aiRecommendation": recommended,
        "pendingByRecommendation": pending_recommendation,
        "sites": [{"siteId": key, "count": count} for key, count in sorted(site_counts.items(), key=lambda item: (-item[1], item[0]))],
        "classifications": [{"value": key, "count": count} for key, count in sorted(classification_counts.items(), key=lambda item: (-item[1], item[0]))[:50]],
    }


AUTO_APPROVAL_POLICY_VERSION = "conservative-equivalence-v1"



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
        where.append("EXISTS (SELECT 1 FROM review_sample_item si WHERE si.sample_id=? AND si.candidate_id=c.candidate_id)")
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
    eligible = int(connection.execute(
        f"SELECT count(*) FROM semantic_candidate c WHERE {where_sql}", parameters
    ).fetchone()[0])
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


def ai_review_preview(scope: Literal["sample", "batch"] = "sample") -> dict[str, Any]:
    sqlite = sqlite_connection()
    try:
        return auto_approval_preview(sqlite, scope)
    finally:
        sqlite.close()


def ai_agent_review_preview(
    batch_size: int = Query(default=CANDIDATE_AGENT_DEFAULT_BATCH_SIZE, ge=10, le=CANDIDATE_AGENT_MAX_BATCH_SIZE),
) -> dict[str, Any]:
    """Show the next AI-review workload without calling the model or writing state."""
    sqlite = sqlite_connection()
    try:
        batch = latest_batch(sqlite)
        return candidate_agent_preview(sqlite, batch, batch_size)
    finally:
        sqlite.close()


def ai_auto_approve(request: AutoApprovalRequest, actor: str = Depends(require_decision_auth)) -> dict[str, Any]:
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
            f"""
            SELECT c.candidate_id, c.candidate_description
            FROM semantic_candidate c
            WHERE {where_sql}
            ORDER BY c.candidate_id
            """,
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
            receipt = f"receipt-{uuid.uuid4().hex}"
            sqlite.execute(
                """
                INSERT INTO review_decision
                  (review_id,candidate_id,decision,reviewed_description,reason_code,review_note,reviewer,approval_receipt,reviewed_at)
                VALUES (?,?,?,?,?,?,?,?,?)
                """,
                (
                    review_id,
                    row["candidate_id"],
                    "approved",
                    row["candidate_description"],
                    "AI_AUTO_APPROVE_EQUIVALENT",
                    f"AI 辅助审批；策略 {AUTO_APPROVAL_POLICY_VERSION}；原描述与候选描述一致，未改写源数据。",
                    "ai-gate",
                    receipt,
                    now,
                ),
            )
            sqlite.execute(
                "UPDATE semantic_candidate SET review_state='approved' WHERE candidate_id=?",
                (row["candidate_id"],),
            )
            sqlite.execute(
                """
                INSERT INTO audit_event(entity_type,entity_id,event_type,actor,payload_json,event_at)
                VALUES (?,?,?,?,?,?)
                """,
                (
                    "review",
                    review_id,
                    "ai_auto_approved",
                    "ai-gate",
                    json.dumps({
                        "candidate_id": row["candidate_id"],
                        "policy_version": AUTO_APPROVAL_POLICY_VERSION,
                        "scope": request.scope,
                        "run_id": run_id,
                        "operator": actor,
                    }, ensure_ascii=False),
                    now,
                ),
            )
            applied += 1

        sqlite.execute(
            """
            UPDATE batch_run SET
              needs_review_count=(SELECT count(*) FROM semantic_candidate WHERE batch_id=? AND review_state='pending'),
              approved_count=(SELECT count(*) FROM semantic_candidate WHERE batch_id=? AND review_state IN ('approved','modified'))
            WHERE batch_id=?
            """,
            (batch["batch_id"], batch["batch_id"], batch["batch_id"]),
        )
        sqlite.execute(
            """
            UPDATE review_sample
            SET status=CASE WHEN NOT EXISTS (
              SELECT 1 FROM review_sample_item i
              JOIN semantic_candidate c ON c.candidate_id=i.candidate_id
              WHERE i.sample_id=review_sample.sample_id AND c.review_state='pending'
            ) THEN 'completed' ELSE status END
            WHERE sample_id=?
            """,
            (sample_id,) if sample_id else ("",),
        )
        sqlite.execute(
            """
            INSERT INTO ai_review_run
              (run_id,idempotency_key,batch_id,scope,policy_version,eligible_count,applied_count,skipped_count,status,started_at,finished_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            (run_id, request.idempotency_key, batch["batch_id"], request.scope, AUTO_APPROVAL_POLICY_VERSION, len(eligible_rows), applied, skipped, "completed", now, now),
        )
        sqlite.execute(
            """
            INSERT INTO audit_event(entity_type,entity_id,event_type,actor,payload_json,event_at)
            VALUES (?,?,?,?,?,?)
            """,
            (
                "ai_review_run",
                run_id,
                "ai_auto_approval_completed",
                "ai-gate",
                json.dumps({"scope": request.scope, "eligible_count": len(eligible_rows), "applied_count": applied, "skipped_count": skipped, "policy_version": AUTO_APPROVAL_POLICY_VERSION, "operator": actor}, ensure_ascii=False),
                now,
            ),
        )
        sqlite.commit()
        return {
            "runId": run_id,
            "batchId": batch["batch_id"],
            "scope": request.scope,
            "policyVersion": AUTO_APPROVAL_POLICY_VERSION,
            "eligibleCount": len(eligible_rows),
            "appliedCount": applied,
            "skippedCount": skipped,
            "status": "completed",
            "replayed": False,
        }
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


def agent_audit_pending_candidates(request: AgentCandidateReviewRequest, actor: str = Depends(require_decision_auth)) -> dict[str, Any]:
    """Audit one bounded high-quality slice with the model.

    The agent may only approve retention of the source description. Every
    other answer is isolated as ``deferred`` for later human review; it never
    becomes an automatic rejection or publication.
    """
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
        where_sql, parameters = candidate_agent_where(
            batch["batch_id"],
            candidate_ids=request.candidate_ids,
        )
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
                existing_review = sqlite.execute(
                    "SELECT review_id FROM review_decision WHERE candidate_id=?",
                    (row["candidate_id"],),
                ).fetchone()
                if existing_review is not None:
                    skipped_count += 1
                    continue
                review_id = f"review-{uuid.uuid4().hex}"
                receipt = f"receipt-{uuid.uuid4().hex}"
                note = json.dumps(
                    {
                        "agentDecision": item["agentDecision"],
                        "confidence": item["confidence"],
                        "risk": item["risk"],
                        "reason": item["reason"],
                        "localGate": item["localGate"],
                        "policyVersion": CANDIDATE_AGENT_REVIEW_VERSION,
                    },
                    ensure_ascii=False,
                )
                sqlite.execute(
                    "INSERT INTO review_decision(review_id,candidate_id,decision,reviewed_description,reason_code,review_note,reviewer,approval_receipt,reviewed_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (review_id, row["candidate_id"], "approved", row["original_description"], "AI_AGENT_KEEP_ORIGINAL", note, "semantic-review-agent", receipt, now),
                )
                sqlite.execute(
                    "UPDATE semantic_candidate SET review_state='approved' WHERE candidate_id=?",
                    (row["candidate_id"],),
                )
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
                # Do not create a review_decision here.  The deferred state is
                # an AI isolation marker, so a human can still submit the
                # first real review decision later without a UNIQUE conflict.
                sqlite.execute(
                    "UPDATE semantic_candidate SET review_state='deferred' WHERE candidate_id=? AND review_state='pending'",
                    (row["candidate_id"],),
                )
                sqlite.execute(
                    "INSERT INTO audit_event(entity_type,entity_id,event_type,actor,payload_json,event_at) VALUES (?,?,?,?,?,?)",
                    ("candidate", row["candidate_id"], "candidate_agent_review_isolated", "semantic-review-agent", json.dumps({"candidateId": row["candidate_id"], **item, "isolation": "deferred", "sourceWrite": False, "formalPublication": False}, ensure_ascii=False), now),
                )
        sqlite.execute(
            "UPDATE batch_run SET needs_review_count=(SELECT count(*) FROM semantic_candidate WHERE batch_id=? AND review_state='pending'), approved_count=(SELECT count(*) FROM semantic_candidate WHERE batch_id=? AND review_state IN ('approved','modified')) WHERE batch_id=?",
            (batch["batch_id"], batch["batch_id"], batch["batch_id"]),
        )
        result = {
            "runId": run_id,
            "batchId": batch["batch_id"],
            "policyVersion": CANDIDATE_AGENT_REVIEW_VERSION,
            "candidateCount": len(rows),
            "approvedCount": approved_count,
            "needsReviewCount": needs_review_count,
            "rejectedCount": rejected_count,
            "isolatedCount": isolated_count,
            "skippedCount": skipped_count,
            "status": "completed",
            "decisions": decisions,
            "sourceWrite": False,
            "formalPublication": False,
        }
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
        result.update({
            "remainingEligibleCount": refreshed_preview["eligibleCount"],
            "remainingPendingCount": refreshed_preview["pendingCount"],
            "isolatedTotalCount": refreshed_preview["isolatedCount"],
        })
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


def ai_review_sample(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    ai_decision: Literal["all", "keep_original", "accept_candidate", "needs_review"] = "all",
    site_id: str | None = None,
) -> dict[str, Any]:
    sample_id, sample_rows = load_ai_review_sample()
    sqlite = sqlite_connection()
    try:
        summary = ai_sample_summary(sqlite, sample_id, sample_rows)
        reviewed = {
            row["candidate_id"]: row
            for row in sqlite.execute(
                "SELECT candidate_id, decision, note, reviewer, reviewed_at FROM ai_review_decision WHERE sample_id=?",
                (sample_id,),
            ).fetchall()
        }
        filtered: list[dict[str, Any]] = []
        for row in sample_rows:
            if site_id and row.get("SITEID") != site_id:
                continue
            existing = reviewed.get(row.get("CANDIDATE_ID", ""))
            current_decision = existing["decision"] if existing else None
            recommended_decision = {"保留原文": "keep_original", "接受候选": "accept_candidate", "需要复核": "needs_review"}.get(row.get("AI_DECISION", ""), "needs_review")
            if ai_decision != "all" and recommended_decision != ai_decision:
                continue
            filtered.append({
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
            })
        start = (page - 1) * page_size
        return {"rows": filtered[start:start + page_size], "total": len(filtered), "page": page, "pageSize": page_size, "summary": summary}
    finally:
        sqlite.close()


def save_ai_review_decision(request: AiDecisionRequest, actor: str = Depends(require_decision_auth)) -> dict[str, Any]:
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


def save_ai_bulk_decision(request: AiBulkDecisionRequest, actor: str = Depends(require_decision_auth)) -> dict[str, Any]:
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


def candidates(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    search: str | None = None,
    site_id: str | None = None,
    classification: str | None = None,
    quick_filter: Literal["all", "pending", "context", "low", "deferred"] = "all",
    sample_only: bool = False,
    sample_size: int = Query(default=DEFAULT_SAMPLE_TARGET, ge=1, le=MAX_SAMPLE_TARGET),
) -> dict[str, Any]:
    sqlite = sqlite_connection()
    try:
        batch = latest_batch(sqlite)
        where = ["c.batch_id = ?"]
        parameters: list[Any] = [batch["batch_id"]]
        if sample_only:
            sample = ensure_review_sample(sqlite, batch, sample_size)
            where.append("EXISTS (SELECT 1 FROM review_sample_item si WHERE si.sample_id=? AND si.candidate_id=c.candidate_id)")
            parameters.append(sample["sample_id"])
        if search and search.strip():
            value = f"%{search.strip()}%"
            where.append("(d.asset_number LIKE ? OR d.original_description LIKE ? OR c.candidate_description LIKE ? OR d.location_code LIKE ? OR d.location_description LIKE ?)")
            parameters.extend([value] * 5)
        if site_id:
            where.append("d.site_id = ?")
            parameters.append(site_id)
        if classification:
            where.append("d.classification_description = ?")
            parameters.append(classification)
        if quick_filter == "pending":
            where.append("c.review_state = 'pending'")
        elif quick_filter == "context":
            where.append("(c.reason_codes_json LIKE '%LOCATION%' OR c.reason_codes_json LIKE '%CONTEXT%')")
        elif quick_filter == "low":
            where.append("c.confidence = 'low'")
        elif quick_filter == "deferred":
            where.append("c.review_state = 'deferred'")
        where_sql = " AND ".join(where)
        total = sqlite.execute(
            f"SELECT count(*) FROM semantic_candidate c JOIN device_identity d ON d.device_id=c.device_id WHERE {where_sql}",
            parameters,
        ).fetchone()[0]
        parameters.extend([page_size, (page - 1) * page_size])
        rows = sqlite.execute(
            f"""
            SELECT c.candidate_id,c.batch_id,d.site_id,d.asset_number,c.original_description,
              c.candidate_description,d.location_code,d.location_description,d.location_parent,
              d.classification_description,c.confidence,c.validator_status,c.review_state,
              c.reason_codes_json,c.evidence_level,c.created_at
            FROM semantic_candidate c JOIN device_identity d ON d.device_id=c.device_id
            WHERE {where_sql}
            ORDER BY d.site_id, d.asset_number, c.candidate_id
            LIMIT ? OFFSET ?
            """,
            parameters,
        ).fetchall()
        return {"rows": [row_to_candidate(row) for row in rows], "total": int(total), "page": page, "pageSize": page_size}
    finally:
        sqlite.close()


def candidate_facets(
    quick_filter: Literal["all", "pending", "context", "low", "deferred"] = "all",
    sample_only: bool = False,
    sample_size: int = Query(default=DEFAULT_SAMPLE_TARGET, ge=1, le=MAX_SAMPLE_TARGET),
) -> dict[str, Any]:
    """Return filter values from the current local batch, never hard-coded UI values."""
    sqlite = sqlite_connection()
    try:
        batch = latest_batch(sqlite)
        where = ["c.batch_id=?"]
        parameters: list[Any] = [batch["batch_id"]]
        if sample_only:
            sample = ensure_review_sample(sqlite, batch, sample_size)
            where.append("EXISTS (SELECT 1 FROM review_sample_item si WHERE si.sample_id=? AND si.candidate_id=c.candidate_id)")
            parameters.append(sample["sample_id"])
        if quick_filter == "pending":
            where.append("c.review_state='pending'")
        elif quick_filter == "deferred":
            where.append("c.review_state='deferred'")
        elif quick_filter == "low":
            where.append("c.confidence='low'")
        elif quick_filter == "context":
            where.append("(c.reason_codes_json LIKE '%LOCATION%' OR c.reason_codes_json LIKE '%CONTEXT%')")
        where_sql = " AND ".join(where)
        sites = sqlite.execute(
            f"SELECT COALESCE(NULLIF(trim(d.site_id),''),'未填写') AS value,count(*) AS count FROM semantic_candidate c JOIN device_identity d ON d.device_id=c.device_id WHERE {where_sql} GROUP BY value ORDER BY count DESC,value",
            parameters,
        ).fetchall()
        classifications = sqlite.execute(
            f"SELECT COALESCE(NULLIF(trim(d.classification_description),''),'未分类') AS value,count(*) AS count FROM semantic_candidate c JOIN device_identity d ON d.device_id=c.device_id WHERE {where_sql} GROUP BY value ORDER BY count DESC,value LIMIT 50",
            parameters,
        ).fetchall()
        return {
            "batchId": batch["batch_id"],
            "sites": [{"value": row["value"], "count": int(row["count"])} for row in sites],
            "classifications": [{"value": row["value"], "count": int(row["count"])} for row in classifications],
            "sourceWrite": False,
            "formalPublication": False,
        }
    finally:
        sqlite.close()


def candidate_detail(candidate_id: str) -> dict[str, Any]:
    sqlite = sqlite_connection()
    try:
        row = sqlite.execute(
            """
            SELECT c.candidate_id,c.batch_id,c.original_description,c.candidate_description,c.confidence,
              c.validator_status,c.review_state,c.reason_codes_json,c.evidence_level,c.applied_rule_ids_json,
              c.rule_version,c.validator_version,c.created_at,d.source_asset_id,d.site_id,d.asset_number,
              d.source_row_hash,d.context_hash,d.location_code,d.location_description,d.location_parent,
              d.classification_description
            FROM semantic_candidate c JOIN device_identity d ON d.device_id=c.device_id
            WHERE c.candidate_id=?
            """,
            (candidate_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="候选记录不存在")
        result = row_to_candidate(row)
        result.update({
            "assetId": row["source_asset_id"] or "",
            "sourceRowHash": row["source_row_hash"],
            "contextHash": row["context_hash"],
            "specificationCount": 0,
            "featureCount": 0,
            "parentChildEvidence": "当前 SQLite 流程库只保存摘要；详细上下文从 DuckDB 分析库读取。",
            "appliedRules": parse_json_array(row["applied_rule_ids_json"]),
            "validatorVersion": row["validator_version"],
            "ruleVersion": row["rule_version"],
        })
        return result
    finally:
        sqlite.close()


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

