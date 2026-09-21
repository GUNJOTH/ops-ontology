"""Proposal normalization, review gates and lifecycle-side processing.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from app.core.config import RULE_AGENT_MAX_PROPOSALS
from app.core.utils import utc_now

from .canonicalization import context_rule_spec_from_agent
from .replay import (
    replay_rule_agent_against_evaluation_cases,
    rule_agent_proposal_row,
    rule_agent_target_rows,
    rule_agent_write_preview,
)


def canonicalize_rule_agent_proposals(
    proposals: list[dict[str, Any]],
    catalog: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool]:
    """Bind AI output to the locally mined executable rule catalog."""
    catalog_by_key = {str(item["patternKey"]): item for item in catalog}
    selected: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    used: set[str] = set()
    for item in proposals:
        pattern_key = str(item.get("patternKey") or "").strip()
        spec = catalog_by_key.get(pattern_key)
        if spec is None and pattern_key.startswith("context."):
            spec = context_rule_spec_from_agent(item, pattern_key)
        if spec is None:
            rejected.append(
                {
                    "patternKey": pattern_key,
                    "reason": "context_dsl_validation_failed" if pattern_key.startswith("context.") else "unsupported_or_missing_pattern_key",
                    "operation": item.get("operation"),
                    "condition": item.get("condition"),
                    "parameters": item.get("parameters"),
                    "scope": item.get("scope"),
                }
            )
            continue
        if pattern_key in used:
            rejected.append({"patternKey": pattern_key, "reason": "duplicate_pattern_key"})
            continue
        used.add(pattern_key)
        try:
            model_confidence = max(0.0, min(1.0, float(item.get("confidence") or 0)))
        except (TypeError, ValueError):
            model_confidence = 0.0
        risk_order = {"low": 0, "medium": 1, "high": 2}
        model_risk = str(item.get("riskLevel") or spec["riskLevel"]).lower()
        if model_risk not in risk_order:
            model_risk = spec["riskLevel"]
        risk = max((spec["riskLevel"], model_risk), key=lambda value: risk_order[value])
        ai_evidence = item.get("evidence") if isinstance(item.get("evidence"), dict) else {}
        selected.append(
            {
                **item,
                "patternKey": pattern_key,
                "ruleKey": spec["ruleKey"],
                "title": str(item.get("title") or spec["title"])[:200],
                "objective": str(item.get("objective") or spec["objective"])[:1000],
                "operation": spec["operation"],
                "condition": spec["condition"],
                "parameters": spec["parameters"],
                "scope": {},
                "evidence": {
                    "reason": str(ai_evidence.get("reason") or "")[:1000],
                    "matchedCount": spec["matchedCount"],
                    "sampleCount": spec["sampleCount"],
                    "measuredFromLocalPreview": True,
                    "evidenceSource": spec["evidenceSource"],
                    "catalogVersion": spec["catalogVersion"],
                    "patternKey": pattern_key,
                },
                "examples": spec["examples"],
                "expectedCount": spec["matchedCount"],
                "confidence": min(model_confidence, float(spec["confidence"])) if model_confidence else 0.0,
                "riskLevel": risk,
            }
        )

    fallback_used = False
    if not selected:
        fallback_used = bool(catalog)
        for spec in catalog[:RULE_AGENT_MAX_PROPOSALS]:
            selected.append(
                {
                    "patternKey": spec["patternKey"],
                    "ruleKey": spec["ruleKey"],
                    "title": spec["title"],
                    "objective": spec["objective"],
                    "operation": spec["operation"],
                    "condition": spec["condition"],
                    "parameters": spec["parameters"],
                    "scope": {},
                    "evidence": {
                        "reason": "local deterministic catalog fallback",
                        "matchedCount": spec["matchedCount"],
                        "sampleCount": spec["sampleCount"],
                        "measuredFromLocalPreview": True,
                        "evidenceSource": spec["evidenceSource"],
                        "catalogVersion": spec["catalogVersion"],
                        "patternKey": spec["patternKey"],
                    },
                    "examples": spec["examples"],
                    "expectedCount": spec["matchedCount"],
                    "confidence": float(spec["confidence"]),
                    "riskLevel": spec["riskLevel"],
                    "agentGenerated": False,
                }
            )
    return selected[:RULE_AGENT_MAX_PROPOSALS], rejected, fallback_used


def deterministic_rule_review_gate(row: sqlite3.Row, model_review: dict[str, Any]) -> tuple[str, float, str]:
    """Constrain an AI recommendation before it is shown as auto-acceptable."""
    raw_decision = str(model_review.get("decision") or "needs_review").strip().lower()
    confidence = max(0.0, min(1.0, float(model_review.get("confidence") or 0)))
    reason = str(model_review.get("reason") or "智能体未提供充分理由").strip()[:1000]
    if raw_decision in {"reject", "rejected"}:
        return "reject", confidence, reason
    checks: list[str] = []
    operation = str(row["operation"] or "").strip().lower()
    if operation not in {"replace", "normalize", "trim"}:
        checks.append("仅允许 replace/normalize/trim 自动处理")
    if row["risk_level"] != "low":
        checks.append("风险等级不是 low")
    if float(row["confidence"] or 0) < 0.85:
        checks.append("规则草案置信度低于 0.85")
    if int(row["expected_count"] or 0) <= 0:
        checks.append("预计影响数量为 0")
    examples = json.loads(row["examples_json"] or "[]")
    if not isinstance(examples, list) or len(examples) < 2:
        checks.append("有效前后样本少于 2 条")
    if operation == "replace":
        parameters = json.loads(row["parameters_json"] or "{}")
        if not str(parameters.get("from") or parameters.get("source") or "").strip():
            checks.append("replace 缺少明确的 from/source")
    if raw_decision not in {"accept_rule", "accept", "approve", "approved"}:
        checks.append("智能体建议需要复核")
    if confidence < 0.85:
        checks.append("智能体审核置信度低于 0.85")
    if checks:
        return "needs_review", confidence, f"{reason}；" + "；".join(checks)
    return "accept_rule", confidence, reason



def auto_process_accepted_rule_agent_proposal(
    connection: sqlite3.Connection,
    proposal: sqlite3.Row,
) -> sqlite3.Row:
    """Replay first; write a preview only after every active case passes."""
    target_rows = rule_agent_target_rows(connection, proposal)
    if not target_rows:
        return proposal
    replay_id = f"replay-agent-eval-{proposal['proposal_id']}-{hashlib.sha256(proposal['rule_version'].encode()).hexdigest()[:12]}"
    evaluation = replay_rule_agent_against_evaluation_cases(connection, proposal, replay_id)
    now = utc_now()
    status = "replayed" if evaluation["status"] == "passed" else "failed"
    preview_path: Path | None = None
    sample_path: Path | None = None
    preview_sha: str | None = None
    if status == "replayed":
        preview_path, sample_path, preview_sha = rule_agent_write_preview(proposal, target_rows)
    connection.execute(
        """
        UPDATE rule_agent_proposal
        SET status=?,preview_path=?,sample_path=?,preview_sha256=?,preview_count=?,
            replay_count=?,replay_pass_count=?,replay_fail_count=?,
            evaluation_replay_id=?,evaluation_count=?,evaluation_pass_count=?,evaluation_fail_count=?,
            replay_message=?,updated_at=?
        WHERE proposal_id=?
        """,
        (
            status,
            str(preview_path) if preview_path else None,
            str(sample_path) if sample_path else None,
            preview_sha,
            len(target_rows) if status == "replayed" else 0,
            len(target_rows) if status == "replayed" else 0,
            len(target_rows) if status == "replayed" else 0,
            0 if status == "replayed" else len(target_rows),
            evaluation["replayId"],
            evaluation["evaluationCount"],
            evaluation["passCount"],
            evaluation["failCount"],
            None if status == "replayed" else json.dumps(evaluation["failures"][:50], ensure_ascii=False),
            now,
            proposal["proposal_id"],
        ),
    )
    connection.execute(
        "INSERT INTO audit_event(entity_type,entity_id,event_type,actor,payload_json,event_at) VALUES (?,?,?,?,?,?)",
        (
            "rule_agent_proposal",
            proposal["proposal_id"],
            "rule_agent_auto_preview_replay_completed",
            "rule-agent-review",
            json.dumps(
                {
                    "proposalId": proposal["proposal_id"],
                    "candidateMatchCount": len(target_rows),
                    "previewCount": len(target_rows) if status == "replayed" else 0,
                    "evaluationReplayId": evaluation["replayId"],
                    "evaluationCount": evaluation["evaluationCount"],
                    "evaluationPassCount": evaluation["passCount"],
                    "evaluationFailCount": evaluation["failCount"],
                    "status": status,
                    "sourceWrite": False,
                    "formalPublication": False,
                },
                ensure_ascii=False,
            ),
            now,
        ),
    )
    return rule_agent_proposal_row(connection, proposal["proposal_id"])



def rule_agent_proposal_payload(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "proposalId": row["proposal_id"],
        "runId": row["run_id"],
        "ruleKey": row["rule_key"],
        "ruleVersion": row["rule_version"],
        "title": row["title"],
        "objective": row["objective"],
        "operation": row["operation"],
        "condition": json.loads(row["condition_json"] or "{}"),
        "parameters": json.loads(row["parameters_json"] or "{}"),
        "scope": json.loads(row["scope_json"] or "{}"),
        "evidence": json.loads(row["evidence_json"] or "{}"),
        "examples": json.loads(row["examples_json"] or "[]"),
        "expectedCount": int(row["expected_count"]),
        "confidence": float(row["confidence"]),
        "riskLevel": row["risk_level"],
        "status": row["status"],
        "discoveryFilterStatus": row["discovery_filter_status"],
        "discoveryFilterReason": row["discovery_filter_reason"],
        "discoveryFilteredAt": row["discovery_filtered_at"],
        "previewPath": row["preview_path"],
        "samplePath": row["sample_path"],
        "previewCount": int(row["preview_count"]),
        "replayCount": int(row["replay_count"]),
        "replayPassCount": int(row["replay_pass_count"]),
        "replayFailCount": int(row["replay_fail_count"]),
        "evaluationReplayId": row["evaluation_replay_id"],
        "evaluationCount": int(row["evaluation_count"] or 0),
        "evaluationPassCount": int(row["evaluation_pass_count"] or 0),
        "evaluationFailCount": int(row["evaluation_fail_count"] or 0),
        "agentReviewDecision": row["agent_review_decision"],
        "agentReviewConfidence": float(row["agent_review_confidence"] or 0),
        "agentReviewReason": row["agent_review_reason"],
        "agentReviewVersion": row["agent_review_version"],
        "agentReviewedAt": row["agent_reviewed_at"],
        "replayMessage": row["replay_message"],
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }
