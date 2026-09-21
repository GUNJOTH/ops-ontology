"""AI invocation and strict JSON normalization for candidate review."""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from fastapi import HTTPException

from app.agent.client import (
    RuleAgentCallError,
    invoke_rule_agent_completion,
    parse_rule_agent_json,
    rule_agent_failure_detail,
)

from .common import CANDIDATE_AGENT_EVIDENCE_LEVELS


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
