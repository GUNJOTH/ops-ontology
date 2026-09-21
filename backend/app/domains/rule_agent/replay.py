"""Local targeting, preview and deterministic replay services.
"""
from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path
from typing import Any

from fastapi import HTTPException
from semantic_lib import sha256_file

from app.core.config import RULE_AGENT_DIR
from app.core.utils import utc_now

from .constants import (
    RULE_AGENT_EVIDENCE_SQL,
    RULE_AGENT_MIN_EVIDENCE_SAMPLES,
    RULE_AGENT_MIN_MATCH_COUNT,
)
from .engine import rule_agent_scope_matches, rule_agent_stratified_sample, rule_agent_transform


def rule_agent_target_rows(connection: sqlite3.Connection, proposal: sqlite3.Row | dict[str, Any]) -> list[dict[str, Any]]:
    batch = connection.execute("SELECT batch_id FROM batch_run WHERE batch_id=(SELECT batch_id FROM rule_agent_run WHERE run_id=?)", (proposal["run_id"],)).fetchone()
    if batch is None:
        raise HTTPException(status_code=409, detail="规则草案关联的批次不存在")
    rows = connection.execute(
        f"""
        SELECT c.candidate_id,c.original_description,c.candidate_description,
          d.site_id,d.asset_number,d.location_code,d.location_description,
          d.location_parent,d.classification_description
        FROM semantic_candidate c JOIN device_identity d ON d.device_id=c.device_id
        WHERE c.batch_id=? AND c.confidence='high' AND {RULE_AGENT_EVIDENCE_SQL}
          AND c.validator_status='candidate'
          AND c.review_state='pending' AND c.publication_state='unpublished'
          AND length(trim(c.original_description)) > 0
        ORDER BY d.site_id,COALESCE(d.classification_description,''),c.candidate_id
        """,
        (batch["batch_id"],),
    ).fetchall()
    condition = json.loads(proposal["condition_json"] or "{}")
    parameters = json.loads(proposal["parameters_json"] or "{}")
    scope = json.loads(proposal["scope_json"] or "{}")
    target_rows: list[dict[str, Any]] = []
    for row in rows:
        if not rule_agent_scope_matches(row, scope):
            continue
        matched, transformed, reason = rule_agent_transform(row["original_description"] or "", proposal["operation"], condition, parameters)
        if not matched:
            continue
        target_rows.append({
            "CANDIDATE_ID": row["candidate_id"],
            "SITEID": row["site_id"] or "",
            "ASSETNUM": row["asset_number"] or "",
            "ORIGINAL_DESCRIPTION": row["original_description"] or "",
            "PROPOSED_DESCRIPTION": transformed,
            "LOCATION": row["location_code"] or "",
            "LOCATION_DESCRIPTION": row["location_description"] or "",
            "LOCATION_PARENT": row["location_parent"] or "",
            "CLASSIFICATION": row["classification_description"] or "",
            "RULE_KEY": proposal["rule_key"],
            "OPERATION": proposal["operation"],
            "MATCH_REASON": reason,
        })
    return target_rows



def measure_rule_agent_proposal(
    connection: sqlite3.Connection,
    run_id: str,
    rule_key: str,
    operation: str,
    condition: dict[str, Any],
    parameters: dict[str, Any],
    scope: dict[str, Any],
) -> dict[str, Any]:
    """Measure a model proposal against local candidates before persisting it."""
    proposal_ref = {
        "run_id": run_id,
        "rule_key": rule_key,
        "operation": operation,
        "condition_json": json.dumps(condition, ensure_ascii=False),
        "parameters_json": json.dumps(parameters, ensure_ascii=False),
        "scope_json": json.dumps(scope, ensure_ascii=False),
    }
    try:
        target_rows = rule_agent_target_rows(connection, proposal_ref)
    except HTTPException as exc:
        return {
            "matchedCount": 0,
            "sampleCount": 0,
            "examples": [],
            "eligible": False,
            "filterReason": f"local_executor_error:{str(exc.detail)[:200]}",
        }

    examples = [
        {
            "before": row["ORIGINAL_DESCRIPTION"],
            "after": row["PROPOSED_DESCRIPTION"],
            "candidateId": row["CANDIDATE_ID"],
            "siteId": row["SITEID"],
        }
        for row in target_rows[:5]
    ]
    matched_count = len(target_rows)
    sample_count = len(examples)
    eligible = matched_count >= RULE_AGENT_MIN_MATCH_COUNT and sample_count >= RULE_AGENT_MIN_EVIDENCE_SAMPLES
    return {
        "matchedCount": matched_count,
        "sampleCount": sample_count,
        "examples": examples,
        "eligible": eligible,
        "filterReason": "local_match_and_sample_evidence" if eligible else "no_local_match_or_sample_evidence",
    }



def enrich_rule_agent_proposal_from_local_preview(
    connection: sqlite3.Connection,
    proposal: sqlite3.Row,
) -> sqlite3.Row:
    """Replace model-estimated impact with measured local preview evidence."""
    try:
        target_rows = rule_agent_target_rows(connection, proposal)
    except HTTPException:
        return proposal
    examples = json.loads(proposal["examples_json"] or "[]")
    if not isinstance(examples, list) or len(examples) < 2:
        examples = [
            {
                "before": item["ORIGINAL_DESCRIPTION"],
                "after": item["PROPOSED_DESCRIPTION"],
                "candidateId": item["CANDIDATE_ID"],
            }
            for item in target_rows[:5]
        ]
    evidence = json.loads(proposal["evidence_json"] or "{}")
    if not isinstance(evidence, dict):
        evidence = {}
    evidence.update({"matchedCount": len(target_rows), "sampleCount": min(200, len(target_rows)), "measuredFromLocalPreview": True})
    connection.execute(
        """
        UPDATE rule_agent_proposal
        SET expected_count=?,examples_json=?,evidence_json=?,updated_at=?
        WHERE proposal_id=?
        """,
        (len(target_rows), json.dumps(examples, ensure_ascii=False), json.dumps(evidence, ensure_ascii=False), utc_now(), proposal["proposal_id"]),
    )
    return rule_agent_proposal_row(connection, proposal["proposal_id"])



def rule_agent_write_preview(proposal: sqlite3.Row, rows: list[dict[str, Any]]) -> tuple[Path, Path, str]:
    directory = RULE_AGENT_DIR / proposal["proposal_id"]
    directory.mkdir(parents=True, exist_ok=True)
    preview_path = directory / "preview.csv"
    sample_path = directory / "sample_200.csv"
    fieldnames = ["CANDIDATE_ID", "SITEID", "ASSETNUM", "ORIGINAL_DESCRIPTION", "PROPOSED_DESCRIPTION", "LOCATION", "LOCATION_DESCRIPTION", "LOCATION_PARENT", "CLASSIFICATION", "RULE_KEY", "OPERATION", "MATCH_REASON"]
    sample_rows = rule_agent_stratified_sample(rows, sample_size=200)
    for path, values in ((preview_path, rows), (sample_path, sample_rows)):
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(values)
    return preview_path, sample_path, sha256_file(preview_path)



def rule_agent_case_scope_matches(case: sqlite3.Row, scope: dict[str, Any]) -> bool:
    """Match an evaluation case using only persisted case context."""
    context: dict[str, Any] = {}
    try:
        parsed = json.loads(case["context_json"] or "{}")
        if isinstance(parsed, dict):
            context = parsed
    except json.JSONDecodeError:
        context = {}
    row = {
        "site_id": case["site_id"] or "",
        "classification_description": context.get("classificationDescription")
        or context.get("classification")
        or context.get("classification_description")
        or "",
        "location_parent": context.get("locationParent") or context.get("location_parent") or "",
        "location_code": context.get("locationCode") or context.get("location_code") or context.get("kks") or "",
    }
    return rule_agent_scope_matches(row, scope)



def evaluate_rule_agent_catalog_spec(
    connection: sqlite3.Connection,
    spec: dict[str, Any],
) -> dict[str, Any]:
    """Reject a mined pattern before AI review if it regresses active cases."""
    cases = connection.execute("SELECT * FROM evaluation_case WHERE active=1 ORDER BY case_id").fetchall()
    failures: list[dict[str, Any]] = []
    pass_count = 0
    for case in cases:
        if not rule_agent_case_scope_matches(case, spec.get("scope") or {}):
            pass_count += 1
            continue
        matched, transformed, reason = rule_agent_transform(
            case["input_description"],
            spec["operation"],
            spec.get("condition") or {},
            spec.get("parameters") or {},
        )
        message: dict[str, Any] | None = None
        if matched and spec["operation"] in {"keep_original", "block"}:
            message = {"reason": "non_transforming_operation_cannot_change_evaluation_case"}
        elif matched and (
            case["expected_decision"] not in {"approved", "modified"}
            or transformed != case["expected_description"]
        ):
            message = {
                "failureType": case["failure_type"],
                "reason": reason,
                "expectedDecision": case["expected_decision"],
                "expectedDescription": case["expected_description"],
                "actualDescription": transformed,
            }
        if message:
            if len(failures) < 20:
                failures.append({"caseId": case["case_id"], "reason": message})
        else:
            pass_count += 1
    return {
        "evaluationCount": len(cases),
        "evaluationPassCount": pass_count,
        "evaluationFailCount": len(failures) if len(failures) < 20 else len(cases) - pass_count,
        "status": "passed" if not failures else "failed",
        "failures": failures,
    }



def replay_rule_agent_against_evaluation_cases(
    connection: sqlite3.Connection,
    proposal: sqlite3.Row,
    replay_id: str,
) -> dict[str, Any]:
    """Replay a proposed rule against every active historical evaluation case."""
    existing = connection.execute(
        "SELECT replay_id,status,evaluation_count,pass_count,fail_count FROM replay_run WHERE replay_id=?",
        (replay_id,),
    ).fetchone()
    if existing is not None:
        return {
            "replayId": existing["replay_id"],
            "status": existing["status"],
            "evaluationCount": int(existing["evaluation_count"]),
            "passCount": int(existing["pass_count"]),
            "failCount": int(existing["fail_count"]),
            "failures": [],
        }

    cases = connection.execute(
        "SELECT * FROM evaluation_case WHERE active=1 ORDER BY case_id"
    ).fetchall()
    operation = str(proposal["operation"] or "").strip().lower()
    condition = json.loads(proposal["condition_json"] or "{}")
    parameters = json.loads(proposal["parameters_json"] or "{}")
    scope = json.loads(proposal["scope_json"] or "{}")
    started_at = utc_now()
    pass_count = 0
    fail_count = 0
    failures: list[dict[str, str]] = []
    results: list[tuple[str, str, str | None, str, str | None]] = []

    for case in cases:
        expected_decision = case["expected_decision"]
        expected_description = case["expected_description"]
        matched = False
        actual_decision = expected_decision
        actual_description = expected_description
        message: str | None = None
        if rule_agent_case_scope_matches(case, scope):
            matched, transformed, reason = rule_agent_transform(
                case["input_description"], operation, condition, parameters
            )
            if matched and operation not in {"keep_original", "block"}:
                actual_decision = "modified" if transformed != case["input_description"] else "approved"
                actual_description = transformed
                if expected_decision not in {"approved", "modified"} or actual_description != expected_description:
                    message = json.dumps(
                        {
                            "failureType": case["failure_type"],
                            "reason": reason,
                            "expectedDecision": expected_decision,
                            "actualDecision": actual_decision,
                            "expectedDescription": expected_description,
                            "actualDescription": actual_description,
                        },
                        ensure_ascii=False,
                    )
            elif matched and operation in {"keep_original", "block"}:
                message = json.dumps(
                    {"reason": "non_transforming_operation_cannot_change_evaluation_case", "operation": operation},
                    ensure_ascii=False,
                )
        outcome = "fail" if message else "pass"
        if outcome == "pass":
            pass_count += 1
        else:
            fail_count += 1
            if len(failures) < 200:
                failures.append({"caseId": case["case_id"], "reason": message or "evaluation_regression"})
        results.append((case["case_id"], actual_decision, actual_description, outcome, message))

    finished_at = utc_now()
    connection.execute(
        """
        INSERT INTO replay_run
          (replay_id,rule_version,validator_version,evaluation_count,pass_count,fail_count,status,started_at,finished_at)
        VALUES (?,?,?,?,?,?,?,?,?)
        """,
        (
            replay_id,
            proposal["rule_version"],
            "hd-semantic-validator-0.3.0",
            len(cases),
            pass_count,
            fail_count,
            "passed" if fail_count == 0 else "failed",
            started_at,
            finished_at,
        ),
    )
    connection.executemany(
        "INSERT INTO replay_result(replay_id,case_id,actual_decision,actual_description,outcome,message) VALUES (?,?,?,?,?,?)",
        [(replay_id, *result) for result in results],
    )
    connection.execute(
        """
        INSERT INTO audit_event(entity_type,entity_id,event_type,actor,payload_json,event_at)
        VALUES (?,?,?,?,?,?)
        """,
        (
            "rule_agent_proposal",
            proposal["proposal_id"],
            "rule_agent_evaluation_replay_completed",
            "rule-agent-gate",
            json.dumps(
                {
                    "proposalId": proposal["proposal_id"],
                    "replayId": replay_id,
                    "evaluationCount": len(cases),
                    "passCount": pass_count,
                    "failCount": fail_count,
                    "sourceWrite": False,
                    "formalPublication": False,
                },
                ensure_ascii=False,
            ),
            finished_at,
        ),
    )
    return {
        "replayId": replay_id,
        "status": "passed" if fail_count == 0 else "failed",
        "evaluationCount": len(cases),
        "passCount": pass_count,
        "failCount": fail_count,
        "failures": failures,
    }



def rule_agent_proposal_row(connection: sqlite3.Connection, proposal_id: str) -> sqlite3.Row:
    row = connection.execute("SELECT * FROM rule_agent_proposal WHERE proposal_id=?", (proposal_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"规则草案不存在：{proposal_id}")
    if row["discovery_filter_status"] != "eligible":
        raise HTTPException(status_code=409, detail="规则草案已被本地证据门禁隔离，不能继续预览、回放或启用")
    return row
