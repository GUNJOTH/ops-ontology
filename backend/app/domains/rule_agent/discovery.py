"""Rule agent and semantic reasoning routes (native APIRouter)."""
from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException

from app.agent.client import (
    rule_agent_failure_fields,
)
from app.core.config import (
    RULE_AGENT_BASE_URL,
    RULE_AGENT_CATALOG_VERSION,
    RULE_AGENT_MODEL,
)
from app.core.db import sqlite_connection
from app.core.utils import utc_now
from app.domains.candidates.service import latest_batch
from app.schemas.rule_agent import (
    RuleAgentReviewRequest,
    RuleAgentRunRequest,
)

from .service import (
    AGENT_REVIEW_VERSION,
    RULE_AGENT_DISCOVERY_FILTER_VERSION,
    RULE_AGENT_MIN_EVIDENCE_SAMPLES,
    RULE_AGENT_MIN_MATCH_COUNT,
    auto_process_accepted_rule_agent_proposal,
    canonicalize_rule_agent_proposals,
    deterministic_rule_review_gate,
    enrich_rule_agent_proposal_from_local_preview,
    invoke_rule_agent,
    invoke_rule_agent_review,
    measure_rule_agent_proposal,
    rule_agent_profile,
    rule_agent_proposal_payload,
    rule_agent_proposal_row,
)


def ai_review_rule_agent_proposals(request: RuleAgentReviewRequest) -> dict[str, Any]:
    """Review rule proposals in one model call and store only recommendations."""
    sqlite = sqlite_connection()
    try:
        prior = sqlite.execute(
            "SELECT payload_json FROM audit_event WHERE event_type='rule_agent_ai_review_completed' ORDER BY event_id DESC LIMIT 50"
        ).fetchall()
        for event in prior:
            try:
                payload = json.loads(event["payload_json"])
            except json.JSONDecodeError:
                continue
            if payload.get("idempotencyKey") == request.idempotency_key:
                return payload

        if request.proposal_ids:
            marks = ",".join("?" for _ in request.proposal_ids)
            rows = sqlite.execute(
                f"SELECT * FROM rule_agent_proposal WHERE discovery_filter_status='eligible' AND proposal_id IN ({marks}) ORDER BY proposal_id",
                tuple(request.proposal_ids),
            ).fetchall()
        else:
            rows = sqlite.execute(
                "SELECT * FROM rule_agent_proposal WHERE discovery_filter_status='eligible' AND status IN ('draft','previewed','replayed') ORDER BY updated_at DESC LIMIT 20"
            ).fetchall()
        if not rows:
            return {
                "status": "empty",
                "reviewedCount": 0,
                "acceptedCount": 0,
                "needsReviewCount": 0,
                "rejectedCount": 0,
                "proposals": [],
                "sourceWrite": False,
                "formalPublication": False,
            }

        rows = [enrich_rule_agent_proposal_from_local_preview(sqlite, row) for row in rows]
        sqlite.commit()
        model_reviews = {str(item.get("proposalId")): item for item in invoke_rule_agent_review(rows)}
        now = utc_now()
        accepted = 0
        needs_review = 0
        rejected = 0
        sqlite.execute("BEGIN IMMEDIATE")
        for row in rows:
            decision, confidence, reason = deterministic_rule_review_gate(
                row,
                model_reviews.get(row["proposal_id"], {"decision": "needs_review", "reason": "智能体未返回该规则的审核结果"}),
            )
            if decision == "accept_rule":
                accepted += 1
            elif decision == "reject":
                rejected += 1
            else:
                needs_review += 1
            sqlite.execute(
                """
                UPDATE rule_agent_proposal
                SET agent_review_decision=?,agent_review_confidence=?,agent_review_reason=?,
                    agent_review_version=?,agent_reviewed_at=?,updated_at=?
                WHERE proposal_id=?
                """,
                (decision, confidence, reason, AGENT_REVIEW_VERSION, now, now, row["proposal_id"]),
            )
        auto_processed = 0
        for row in rows:
            refreshed = rule_agent_proposal_row(sqlite, row["proposal_id"])
            if refreshed["agent_review_decision"] == "accept_rule":
                refreshed = auto_process_accepted_rule_agent_proposal(sqlite, refreshed)
                if refreshed["status"] == "replayed":
                    auto_processed += 1
        payload = {
            "status": "completed",
            "reviewedCount": len(rows),
            "acceptedCount": accepted,
            "needsReviewCount": needs_review,
            "rejectedCount": rejected,
            "autoProcessedCount": auto_processed,
            "idempotencyKey": request.idempotency_key,
            "reviewVersion": AGENT_REVIEW_VERSION,
            "sourceWrite": False,
            "formalPublication": False,
        }
        sqlite.execute(
            "INSERT INTO audit_event(entity_type,entity_id,event_type,actor,payload_json,event_at) VALUES (?,?,?,?,?,?)",
            ("rule_agent", f"ai-review-{uuid.uuid4().hex}", "rule_agent_ai_review_completed", "rule-agent-review", json.dumps({**payload, "note": request.note}, ensure_ascii=False), now),
        )
        sqlite.commit()
        saved = sqlite.execute(
            "SELECT * FROM rule_agent_proposal WHERE proposal_id IN ({}) ORDER BY proposal_id".format(",".join("?" for _ in rows)),
            tuple(row["proposal_id"] for row in rows),
        ).fetchall()
        return {**payload, "proposals": [rule_agent_proposal_payload(row) for row in saved]}
    except HTTPException:
        if sqlite.in_transaction:
            sqlite.rollback()
        raise
    finally:
        sqlite.close()
def discover_rules(request: RuleAgentRunRequest) -> dict[str, Any]:
    sqlite = sqlite_connection()
    run_id = f"rule-agent-{uuid.uuid4().hex}"
    try:
        existing = sqlite.execute("SELECT run_id FROM rule_agent_run WHERE idempotency_key=?", (request.idempotency_key,)).fetchone()
        if existing:
            rows = sqlite.execute("SELECT * FROM rule_agent_proposal WHERE run_id=? ORDER BY proposal_id", (existing["run_id"],)).fetchall()
            audit = sqlite.execute(
                "SELECT payload_json FROM audit_event WHERE entity_id=? AND event_type='rule_agent_discovery_completed' ORDER BY event_id DESC LIMIT 1",
                (existing["run_id"],),
            ).fetchone()
            audit_payload: dict[str, Any] = {}
            if audit:
                try:
                    parsed_audit = json.loads(audit["payload_json"] or "{}")
                    if isinstance(parsed_audit, dict):
                        audit_payload = parsed_audit
                except json.JSONDecodeError:
                    pass
            return {
                "runId": existing["run_id"],
                "status": "replayed",
                "proposals": [rule_agent_proposal_payload(row) for row in rows],
                "proposalCount": len(rows),
                "filterStats": audit_payload.get("filterStats"),
                "sourceWrite": False,
                "formalPublication": False,
            }
        batch = latest_batch(sqlite)
        profile = rule_agent_profile(sqlite, request.sample_size)
        now = utc_now()
        sqlite.execute(
            "INSERT INTO rule_agent_run(run_id,idempotency_key,batch_id,source_snapshot_id,eligible_count,sampled_count,model,provider_base_url,profile_json,status,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, request.idempotency_key, batch["batch_id"], batch["source_snapshot_id"], profile["eligibleCount"], profile["sampleCount"], RULE_AGENT_MODEL, RULE_AGENT_BASE_URL, json.dumps(profile, ensure_ascii=False), "running", now),
        )
        sqlite.commit()
        model_proposals = invoke_rule_agent(profile)
        proposals, dsl_rejected, fallback_used = canonicalize_rule_agent_proposals(
            model_proposals,
            profile.get("localRuleCatalog", []),
        )
        saved: list[dict[str, Any]] = []
        filtered: list[dict[str, Any]] = [
            {
                "ruleKey": item.get("patternKey") or "",
                "title": "AI proposal rejected before local execution",
                "matchedCount": 0,
                "sampleCount": 0,
                "reason": item.get("reason") or "unsupported_or_missing_pattern_key",
                "operation": item.get("operation"),
                "condition": item.get("condition"),
                "parameters": item.get("parameters"),
                "scope": item.get("scope"),
            }
            for item in dsl_rejected
        ]
        for index, item in enumerate(proposals):
            rule_key = re.sub(r"[^a-z0-9_.-]+", "_", str(item.get("ruleKey") or f"agent.discovered.rule_{index + 1}").lower())[:180]
            title = str(item.get("title") or rule_key)[:200]
            operation = str(item.get("operation") or "needs_review")[:40]
            risk = str(item.get("riskLevel") or "high").lower()
            if risk not in {"low", "medium", "high"}:
                risk = "high"
            try:
                confidence = max(0.0, min(1.0, float(item.get("confidence") or 0)))
            except (TypeError, ValueError):
                confidence = 0.0
            condition = item.get("condition") if isinstance(item.get("condition"), dict) else {}
            parameters = item.get("parameters") if isinstance(item.get("parameters"), dict) else {}
            scope = item.get("scope") if isinstance(item.get("scope"), dict) else {}
            measurement = measure_rule_agent_proposal(
                sqlite,
                run_id,
                rule_key,
                operation,
                condition,
                parameters,
                scope,
            )
            if not measurement["eligible"]:
                filtered.append(
                    {
                        "ruleKey": rule_key,
                        "title": title,
                        "matchedCount": measurement["matchedCount"],
                        "sampleCount": measurement["sampleCount"],
                        "reason": measurement["filterReason"],
                    }
                )
                continue
            proposal_id = f"proposal-{run_id}-{index + 1}"
            rule_version = f"{rule_key}-agent-{datetime.now(timezone.utc).strftime('%Y%m%d')}-v1"
            model_evidence = item.get("evidence") if isinstance(item.get("evidence"), dict) else {}
            try:
                model_expected_count = int(item.get("expectedCount") or 0)
            except (TypeError, ValueError):
                model_expected_count = 0
            evidence = {
                **model_evidence,
                "modelExpectedCount": model_expected_count,
                "matchedCount": measurement["matchedCount"],
                "sampleCount": measurement["sampleCount"],
                "measuredFromLocalPreview": True,
                "evidenceSource": "local_candidate_executor",
                "filterVersion": RULE_AGENT_DISCOVERY_FILTER_VERSION,
            }
            sqlite.execute(
                "INSERT OR IGNORE INTO rule_agent_proposal(proposal_id,run_id,rule_key,rule_version,title,objective,operation,condition_json,parameters_json,scope_json,evidence_json,examples_json,expected_count,confidence,risk_level,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (proposal_id, run_id, rule_key, rule_version, title, str(item.get("objective") or "")[:1000], operation, json.dumps(condition, ensure_ascii=False), json.dumps(parameters, ensure_ascii=False), json.dumps(scope, ensure_ascii=False), json.dumps(evidence, ensure_ascii=False), json.dumps(measurement["examples"], ensure_ascii=False), measurement["matchedCount"], confidence, risk, "draft", now, now),
            )
            saved.append({"proposalId": proposal_id, "ruleKey": rule_key})
        sqlite.execute(
            "UPDATE rule_agent_run SET status='completed',finished_at=?,attempt_count=CASE WHEN attempt_count=0 THEN 1 ELSE attempt_count END,fallback_used=? WHERE run_id=?",
            (utc_now(), int(bool(fallback_used)), run_id),
        )
        filter_stats = {
            "version": RULE_AGENT_DISCOVERY_FILTER_VERSION,
            "modelProposalCount": len(model_proposals),
            "eligibleProposalCount": int(sqlite.execute("SELECT count(*) FROM rule_agent_proposal WHERE run_id=?", (run_id,)).fetchone()[0]),
            "filteredProposalCount": len(filtered),
            "dslRejectedProposalCount": len(dsl_rejected),
            "fallbackUsed": fallback_used,
            "catalogVersion": RULE_AGENT_CATALOG_VERSION,
            "catalogBlockedCount": len(profile.get("blockedRuleCatalog", [])),
            "catalogBlocked": profile.get("blockedRuleCatalog", []),
            "minMatchedCount": RULE_AGENT_MIN_MATCH_COUNT,
            "minEvidenceSamples": RULE_AGENT_MIN_EVIDENCE_SAMPLES,
            "filtered": filtered,
        }
        sqlite.execute("INSERT INTO audit_event(entity_type,entity_id,event_type,actor,payload_json,event_at) VALUES (?,?,?,?,?,?)", ("rule_agent_run", run_id, "rule_agent_discovery_completed", "rule-agent", json.dumps({"runId": run_id, "eligibleCount": profile["eligibleCount"], "sampleCount": profile["sampleCount"], "proposalCount": filter_stats["eligibleProposalCount"], "modelProposalCount": len(model_proposals), "filteredProposalCount": len(filtered), "filterStats": filter_stats, "sourceWrite": False, "formalPublication": False}, ensure_ascii=False), utc_now()))
        sqlite.commit()
        rows = sqlite.execute("SELECT * FROM rule_agent_proposal WHERE run_id=? ORDER BY proposal_id", (run_id,)).fetchall()
        return {"runId": run_id, "status": "completed", "profile": profile, "proposals": [rule_agent_proposal_payload(row) for row in rows], "proposalCount": len(rows), "filterStats": filter_stats, "sourceWrite": False, "formalPublication": False}
    except Exception as exc:
        if sqlite.in_transaction:
            sqlite.rollback()
        if isinstance(exc, HTTPException):
            error_message = str(exc.detail)
            error_code, retryable, attempts = rule_agent_failure_fields(exc.detail)
        else:
            error_message = type(exc).__name__
            error_code, retryable, attempts = "unhandled_agent_error", False, 0
        sqlite.execute(
            "UPDATE rule_agent_run SET status='failed',error_message=?,error_code=?,retryable=?,attempt_count=?,finished_at=? WHERE run_id=?",
            (error_message, error_code, int(retryable), attempts, utc_now(), run_id),
        )
        sqlite.commit()
        if isinstance(exc, HTTPException):
            raise
        raise HTTPException(status_code=502, detail=f"规则智能体执行失败：{type(exc).__name__}") from exc
    finally:
        sqlite.close()
