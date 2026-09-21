"""Rule agent and semantic reasoning routes (native APIRouter)."""
from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path
from typing import Any

from fastapi import Depends, HTTPException
from semantic_lib import sha256_file

from app.core.auth import require_decision_auth
from app.core.db import sqlite_connection
from app.core.utils import utc_now
from app.domains.candidates.service import sync_cleaning_runs
from app.schemas.rule_agent import (
    RuleAgentProposalActionRequest,
)

from .service import (
    replay_rule_agent_against_evaluation_cases,
    rule_agent_proposal_payload,
    rule_agent_proposal_row,
    rule_agent_target_rows,
    rule_agent_write_preview,
)


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
