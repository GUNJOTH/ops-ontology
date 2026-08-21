"""Rule agent and semantic reasoning routes (native APIRouter)."""
from __future__ import annotations

import csv
import json
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from semantic_lib import sha256_file

from app.agent.client import (
    rule_agent_failure_fields,
)
from app.core.auth import require_decision_auth
from app.core.config import (
    RULE_AGENT_API_KEY,
    RULE_AGENT_BASE_URL,
    RULE_AGENT_CATALOG_VERSION,
    RULE_AGENT_MAX_ATTEMPTS,
    RULE_AGENT_MODEL,
    RULE_AGENT_TIMEOUT,
)
from app.core.db import sqlite_connection
from app.core.utils import utc_now
from app.domains.candidates.service import latest_batch, sync_cleaning_runs
from app.schemas.rule_agent import (
    RuleAgentProposalActionRequest,
    RuleAgentReviewRequest,
    RuleAgentRunRequest,
    SemanticReasoningRequest,
)

from .service import (
    AGENT_REVIEW_VERSION,
    RULE_AGENT_DISCOVERY_FILTER_VERSION,
    RULE_AGENT_MIN_EVIDENCE_SAMPLES,
    RULE_AGENT_MIN_MATCH_COUNT,
    auto_process_accepted_rule_agent_proposal,
    canonicalize_rule_agent_proposals,
    canonicalize_semantic_reasoning,
    deterministic_rule_review_gate,
    enrich_rule_agent_proposal_from_local_preview,
    invoke_rule_agent,
    invoke_rule_agent_review,
    invoke_semantic_reasoning_agent,
    measure_rule_agent_proposal,
    replay_rule_agent_against_evaluation_cases,
    rule_agent_profile,
    rule_agent_proposal_payload,
    rule_agent_proposal_row,
    rule_agent_target_rows,
    rule_agent_write_preview,
    semantic_reasoning_item_payload,
)


def rule_agent_profile_endpoint(sample_size: int = Query(default=120, ge=20, le=500)) -> dict[str, Any]:
    sqlite = sqlite_connection()
    try:
        return {"profile": rule_agent_profile(sqlite, sample_size), "configured": bool(RULE_AGENT_API_KEY and RULE_AGENT_BASE_URL), "sourceWrite": False}
    finally:
        sqlite.close()


def rule_agent_status() -> dict[str, Any]:
    """Return runtime configuration and recent failure diagnostics without a profile scan."""
    connection = sqlite_connection()
    try:
        latest = connection.execute(
            "SELECT run_id,status,error_code,retryable,attempt_count,error_message,created_at,finished_at "
            "FROM rule_agent_run ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        failed_count = int(connection.execute("SELECT count(*) FROM rule_agent_run WHERE status='failed'").fetchone()[0])
        return {
            "configured": bool(RULE_AGENT_API_KEY and RULE_AGENT_BASE_URL and RULE_AGENT_MODEL),
            "model": RULE_AGENT_MODEL,
            "baseUrlConfigured": bool(RULE_AGENT_BASE_URL),
            "apiKeyConfigured": bool(RULE_AGENT_API_KEY),
            "timeoutSeconds": RULE_AGENT_TIMEOUT,
            "maxAttempts": RULE_AGENT_MAX_ATTEMPTS,
            "failedRunCount": failed_count,
            "latestRun": dict(latest) if latest else None,
            "fallbackPolicy": "仅在模型返回但未绑定本地规则时使用确定性目录；模型调用失败不冒充 AI 结果",
            "sourceWrite": False,
            "formalPublication": False,
        }
    finally:
        connection.close()


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


def rule_agent_proposals(status: Literal["all", "draft", "previewed", "replayed", "confirmed", "enabled", "rejected", "failed"] = "all") -> dict[str, Any]:
    sqlite = sqlite_connection()
    try:
        where = "WHERE discovery_filter_status='eligible'"
        params: tuple[Any, ...] = ()
        if status != "all":
            where += " AND status=?"
            params = (status,)
        rows = sqlite.execute(f"SELECT * FROM rule_agent_proposal {where} ORDER BY updated_at DESC,proposal_id", params).fetchall()
        filtered_count = int(sqlite.execute("SELECT count(*) FROM rule_agent_proposal WHERE discovery_filter_status='filtered'").fetchone()[0])
        runs = sqlite.execute("SELECT run_id,batch_id,source_snapshot_id,eligible_count,sampled_count,model,provider_base_url,status,created_at,finished_at,error_message FROM rule_agent_run ORDER BY created_at DESC LIMIT 20").fetchall()
        return {"proposals": [rule_agent_proposal_payload(row) for row in rows], "filteredProposalCount": filtered_count, "runs": [dict(row) for row in runs], "sourceWrite": False}
    finally:
        sqlite.close()


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


def preview_rule_agent_proposal(proposal_id: str, request: RuleAgentProposalActionRequest) -> dict[str, Any]:
    sqlite = sqlite_connection()
    try:
        proposal = rule_agent_proposal_row(sqlite, proposal_id)
        if proposal["status"] in {"confirmed", "enabled"}:
            raise HTTPException(status_code=409, detail="规则草案已经确认或启用，不能回退重做预览")
        rows = rule_agent_target_rows(sqlite, proposal)
        if not rows:
            raise HTTPException(status_code=409, detail="规则草案在当前高质量范围内没有命中记录")
        preview_path, sample_path, preview_sha = rule_agent_write_preview(proposal, rows)
        now = utc_now()
        sqlite.execute(
            """
            UPDATE rule_agent_proposal
            SET status='previewed',preview_path=?,sample_path=?,preview_sha256=?,preview_count=?,
                replay_count=0,replay_pass_count=0,replay_fail_count=0,
                evaluation_replay_id=NULL,evaluation_count=0,evaluation_pass_count=0,
                evaluation_fail_count=0,replay_message=NULL,updated_at=?
            WHERE proposal_id=?
            """,
            (str(preview_path), str(sample_path), preview_sha, len(rows), now, proposal_id),
        )
        sqlite.execute(
            "INSERT INTO audit_event(entity_type,entity_id,event_type,actor,payload_json,event_at) VALUES (?,?,?,?,?,?)",
            ("rule_agent_proposal", proposal_id, "rule_agent_preview_generated", "local-user", json.dumps({"proposalId": proposal_id, "previewCount": len(rows), "sampleCount": min(200, len(rows)), "sourceWrite": False, "formalPublication": False, "note": request.note}, ensure_ascii=False), now),
        )
        sqlite.commit()
        return {"proposal": rule_agent_proposal_payload(rule_agent_proposal_row(sqlite, proposal_id)), "sourceWrite": False, "formalPublication": False}
    finally:
        sqlite.close()


def replay_rule_agent_proposal(proposal_id: str, request: RuleAgentProposalActionRequest) -> dict[str, Any]:
    sqlite = sqlite_connection()
    try:
        proposal = rule_agent_proposal_row(sqlite, proposal_id)
        if proposal["status"] not in {"previewed", "replayed"}:
            raise HTTPException(status_code=409, detail="必须先生成预览，才能回放")
        preview_path = Path(proposal["preview_path"] or "")
        if not preview_path.exists() or sha256_file(preview_path) != proposal["preview_sha256"]:
            raise HTTPException(status_code=409, detail="预览文件不存在或校验值已变化，请重新生成预览")
        with preview_path.open("r", encoding="utf-8-sig", newline="") as handle:
            preview_rows = list(csv.DictReader(handle))
        current_rows = rule_agent_target_rows(sqlite, proposal)
        current_by_id = {row["CANDIDATE_ID"]: row for row in current_rows}
        failures: list[dict[str, str]] = []
        for item in preview_rows:
            current = current_by_id.get(item.get("CANDIDATE_ID", ""))
            if current is None:
                failures.append({"candidateId": item.get("CANDIDATE_ID", ""), "reason": "scope_changed_or_candidate_missing"})
                continue
            for field in ("ORIGINAL_DESCRIPTION", "PROPOSED_DESCRIPTION", "SITEID", "ASSETNUM"):
                if item.get(field, "") != current.get(field, ""):
                    failures.append({"candidateId": item.get("CANDIDATE_ID", ""), "reason": f"{field.lower()}_changed"})
                    break
        if len(preview_rows) != len(current_rows):
            failures.append({"candidateId": "(scope)", "reason": "preview_current_count_mismatch"})
        preview_failures = list(failures)
        preview_passed = len(preview_rows) - len({item["candidateId"] for item in preview_failures if item["candidateId"] != "(scope)"})
        preview_failed = len(preview_rows) - max(0, preview_passed)
        if any(item["candidateId"] == "(scope)" for item in preview_failures):
            preview_failed = max(preview_failed, 1)
        evaluation: dict[str, Any] = {
            "replayId": None,
            "status": "not_run",
            "evaluationCount": 0,
            "passCount": 0,
            "failCount": 0,
            "failures": [],
        }
        if not preview_failures:
            evaluation_replay_id = f"replay-agent-eval-{proposal_id}-{(proposal['preview_sha256'] or '')[:12]}"
            evaluation = replay_rule_agent_against_evaluation_cases(sqlite, proposal, evaluation_replay_id)
            failures.extend(
                {"candidateId": item["caseId"], "reason": item["reason"]}
                for item in evaluation["failures"]
            )
        status = "passed" if not failures and evaluation["status"] == "passed" else "failed"
        now = utc_now()
        next_status = "replayed" if status == "passed" else "failed"
        sqlite.execute(
            """
            UPDATE rule_agent_proposal
            SET status=?,replay_count=?,replay_pass_count=?,replay_fail_count=?,
                evaluation_replay_id=?,evaluation_count=?,evaluation_pass_count=?,
                evaluation_fail_count=?,replay_message=?,updated_at=?
            WHERE proposal_id=?
            """,
            (
                next_status,
                len(preview_rows),
                preview_passed,
                preview_failed,
                evaluation["replayId"],
                evaluation["evaluationCount"],
                evaluation["passCount"],
                evaluation["failCount"],
                None if status == "passed" else json.dumps(
                    {"previewFailures": preview_failures[:50], "evaluationFailures": evaluation["failures"][:50]},
                    ensure_ascii=False,
                ),
                now,
                proposal_id,
            ),
        )
        sqlite.execute(
            "INSERT INTO audit_event(entity_type,entity_id,event_type,actor,payload_json,event_at) VALUES (?,?,?,?,?,?)",
            ("rule_agent_proposal", proposal_id, "rule_agent_replay_executed", "local-user", json.dumps({"proposalId": proposal_id, "previewEvaluationCount": len(preview_rows), "previewPassCount": preview_passed, "previewFailCount": preview_failed, "evaluationReplayId": evaluation["replayId"], "evaluationCount": evaluation["evaluationCount"], "evaluationPassCount": evaluation["passCount"], "evaluationFailCount": evaluation["failCount"], "status": status, "sourceWrite": False, "formalPublication": False, "note": request.note}, ensure_ascii=False), now),
        )
        sqlite.commit()
        return {"proposal": rule_agent_proposal_payload(rule_agent_proposal_row(sqlite, proposal_id)), "status": status, "failures": failures[:200], "sourceWrite": False, "formalPublication": False}
    finally:
        sqlite.close()


def confirm_rule_agent_proposal(proposal_id: str, request: RuleAgentProposalActionRequest, actor: str = Depends(require_decision_auth)) -> dict[str, Any]:
    sqlite = sqlite_connection()
    try:
        proposal = rule_agent_proposal_row(sqlite, proposal_id)
        if proposal["status"] == "confirmed" or proposal["status"] == "enabled":
            return {"proposal": rule_agent_proposal_payload(proposal), "status": "confirmed", "sourceWrite": False, "formalPublication": False}
        if (
            proposal["status"] != "replayed"
            or proposal["replay_fail_count"] != 0
            or proposal["evaluation_count"] <= 0
            or proposal["evaluation_fail_count"] != 0
        ):
            raise HTTPException(status_code=409, detail="只有回放全部通过的规则草案才能人工确认")
        now = utc_now()
        sqlite.execute("UPDATE rule_agent_proposal SET status='confirmed',updated_at=? WHERE proposal_id=?", (now, proposal_id))
        sqlite.execute(
            "INSERT INTO audit_event(entity_type,entity_id,event_type,actor,payload_json,event_at) VALUES (?,?,?,?,?,?)",
            ("rule_agent_proposal", proposal_id, "rule_agent_proposal_confirmed", actor, json.dumps({"proposalId": proposal_id, "sourceWrite": False, "formalPublication": False, "note": request.note}, ensure_ascii=False), now),
        )
        sqlite.commit()
        return {"proposal": rule_agent_proposal_payload(rule_agent_proposal_row(sqlite, proposal_id)), "status": "confirmed", "sourceWrite": False, "formalPublication": False}
    finally:
        sqlite.close()


def enable_rule_agent_proposal(proposal_id: str, request: RuleAgentProposalActionRequest, actor: str = Depends(require_decision_auth)) -> dict[str, Any]:
    sqlite = sqlite_connection()
    try:
        proposal = rule_agent_proposal_row(sqlite, proposal_id)
        if proposal["status"] == "enabled":
            return {"proposal": rule_agent_proposal_payload(proposal), "status": "enabled", "sourceWrite": False, "formalPublication": False}
        if proposal["status"] != "confirmed":
            raise HTTPException(status_code=409, detail="只有人工确认的规则草案才能启用")
        if proposal["evaluation_count"] <= 0 or proposal["evaluation_fail_count"] != 0 or not proposal["evaluation_replay_id"]:
            raise HTTPException(status_code=409, detail="规则启用前必须完成全量历史评价回放且失败为 0")
        replay_id = proposal["evaluation_replay_id"]
        now = utc_now()
        metadata = {"operation": proposal["operation"], "condition": json.loads(proposal["condition_json"] or "{}"), "parameters": json.loads(proposal["parameters_json"] or "{}"), "scope": json.loads(proposal["scope_json"] or "{}"), "proposalId": proposal_id, "evaluationReplayId": replay_id, "evaluationCount": int(proposal["evaluation_count"]), "evaluationFailCount": int(proposal["evaluation_fail_count"])}
        sqlite.execute(
            "INSERT INTO cleaning_rule_registry(rule_key,cleaning_type,rule_label,action_label,is_cleaning,replay_id,rule_version,enabled,metadata_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (proposal["rule_key"], "agent_" + proposal["operation"], proposal["title"], "规则启用", 1, replay_id, proposal["rule_version"], 1, json.dumps(metadata, ensure_ascii=False), now, now),
        )
        sqlite.execute(
            "INSERT INTO cleaning_run(cleaning_run_id,rule_key,replay_id,status,source_type,stage,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (f"cleaning-run-{replay_id}", proposal["rule_key"], replay_id, "approved", "rule_agent", "approved", now, now),
        )
        sqlite.execute("UPDATE rule_agent_proposal SET status='enabled',enabled_rule_key=?,updated_at=? WHERE proposal_id=?", (proposal["rule_key"], now, proposal_id))
        sqlite.execute(
            "INSERT INTO audit_event(entity_type,entity_id,event_type,actor,payload_json,event_at) VALUES (?,?,?,?,?,?)",
            ("rule_agent_proposal", proposal_id, "rule_agent_rule_enabled", actor, json.dumps({"proposalId": proposal_id, "ruleKey": proposal["rule_key"], "replayId": replay_id, "sourceWrite": False, "formalPublication": False, "note": request.note}, ensure_ascii=False), now),
        )
        sqlite.commit()
        return {"proposal": rule_agent_proposal_payload(rule_agent_proposal_row(sqlite, proposal_id)), "status": "enabled", "replayId": replay_id, "sourceWrite": False, "formalPublication": False}
    except sqlite3.IntegrityError as exc:
        if sqlite.in_transaction:
            sqlite.rollback()
        raise HTTPException(status_code=409, detail=f"规则启用冲突：{exc}") from exc
    finally:
        sqlite.close()


def queue_rule_agent_proposal(proposal_id: str, request: RuleAgentProposalActionRequest, actor: str = Depends(require_decision_auth)) -> dict[str, Any]:
    """Send an enabled rule's current candidates into the common approval queue."""
    sqlite = sqlite_connection()
    try:
        prior = sqlite.execute(
            "SELECT payload_json FROM audit_event WHERE entity_type='rule_agent_proposal' AND entity_id=? AND event_type='rule_agent_candidates_queued' ORDER BY event_id DESC LIMIT 50",
            (proposal_id,),
        ).fetchall()
        for event in prior:
            try:
                payload = json.loads(event["payload_json"])
            except json.JSONDecodeError:
                continue
            if payload.get("idempotencyKey") == request.idempotency_key:
                return payload

        proposal = rule_agent_proposal_row(sqlite, proposal_id)
        if proposal["status"] != "enabled":
            raise HTTPException(status_code=409, detail="规则必须先完成回放、人工确认并启用，才能进入统一审批队列")
        replay = sqlite.execute(
            "SELECT status,fail_count FROM replay_run WHERE replay_id=?",
            (proposal["evaluation_replay_id"],),
        ).fetchone()
        if replay is None or replay["status"] != "passed" or int(replay["fail_count"] or 0) != 0:
            raise HTTPException(status_code=409, detail="规则历史评价回放未通过，不能进入审批队列")

        target_rows = rule_agent_target_rows(sqlite, proposal)
        if not target_rows:
            raise HTTPException(
                status_code=409,
                detail="当前规则关联的批次已没有待审批候选；请针对当前批次重新生成预览并回放后再入队",
            )
        now = utc_now()
        applied = 0
        skipped = 0
        existing_queue_ids = {
            row[0] for row in sqlite.execute("SELECT candidate_id FROM formal_approval_queue").fetchall()
        }
        sqlite.execute("BEGIN IMMEDIATE")
        for item in target_rows:
            candidate_id = item["CANDIDATE_ID"]
            if (
                candidate_id in existing_queue_ids
                or not str(item["PROPOSED_DESCRIPTION"] or "").strip()
            ):
                skipped += 1
                continue
            decision = "modified" if item["PROPOSED_DESCRIPTION"] != item["ORIGINAL_DESCRIPTION"] else "approved"
            sqlite.execute(
                """
                INSERT INTO formal_approval_queue
                  (queue_id,candidate_id,cluster_id,replay_id,proposed_decision,proposed_description,status,note,created_at,updated_at,source_write,formal_publication)
                VALUES (?,?,?,?,?,?,?,?,?,?,0,0)
                """,
                (
                    f"queue-rule-agent-{proposal_id}-{candidate_id}",
                    candidate_id,
                    proposal["rule_key"],
                    proposal["evaluation_replay_id"],
                    decision,
                    item["PROPOSED_DESCRIPTION"],
                    "pending",
                    f"规则智能体候选；规则版本 {proposal['rule_version']}；待人工审批",
                    now,
                    now,
                ),
            )
            applied += 1
        sync_cleaning_runs(sqlite, {proposal["evaluation_replay_id"]: dict(sqlite.execute("SELECT rule_key,cleaning_type,rule_label,action_label,is_cleaning,replay_id,rule_version,enabled FROM cleaning_rule_registry WHERE replay_id=?", (proposal["evaluation_replay_id"],)).fetchone())})
        payload = {
            "proposalId": proposal_id,
            "replayId": proposal["evaluation_replay_id"],
            "targetCount": len(target_rows),
            "appliedCount": applied,
            "skippedCount": skipped,
            "status": "queued",
            "idempotencyKey": request.idempotency_key,
            "sourceWrite": False,
            "formalPublication": False,
        }
        sqlite.execute(
            "INSERT INTO audit_event(entity_type,entity_id,event_type,actor,payload_json,event_at) VALUES (?,?,?,?,?,?)",
            ("rule_agent_proposal", proposal_id, "rule_agent_candidates_queued", actor, json.dumps(payload, ensure_ascii=False), now),
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
        raise HTTPException(status_code=409, detail=f"规则候选进入审批队列冲突：{exc}") from exc
    finally:
        sqlite.close()



def build_router() -> APIRouter:
    router = APIRouter()
    router.add_api_route("/api/rule-agent/profile", rule_agent_profile_endpoint, methods=["GET"])
    router.add_api_route("/api/rule-agent/status", rule_agent_status, methods=["GET"])
    router.add_api_route("/api/semantic-reasoning/analyze", semantic_reasoning_analyze, methods=["POST"])
    router.add_api_route("/api/semantic-reasoning/latest", semantic_reasoning_latest, methods=["GET"])
    router.add_api_route("/api/rule-agent/proposals", rule_agent_proposals, methods=["GET"])
    router.add_api_route("/api/rule-agent/ai-review", ai_review_rule_agent_proposals, methods=["POST"])
    router.add_api_route("/api/rule-agent/discover", discover_rules, methods=["POST"])
    router.add_api_route("/api/rule-agent/proposals/{proposal_id}/preview", preview_rule_agent_proposal, methods=["POST"])
    router.add_api_route("/api/rule-agent/proposals/{proposal_id}/replay", replay_rule_agent_proposal, methods=["POST"])
    router.add_api_route("/api/rule-agent/proposals/{proposal_id}/confirm", confirm_rule_agent_proposal, methods=["POST"])
    router.add_api_route("/api/rule-agent/proposals/{proposal_id}/enable", enable_rule_agent_proposal, methods=["POST"])
    router.add_api_route("/api/rule-agent/proposals/{proposal_id}/queue", queue_rule_agent_proposal, methods=["POST"])
    return router
