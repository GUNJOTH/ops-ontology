"""Cleaning approval, publication and lifecycle orchestration."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import Depends, HTTPException

from app.core.auth import require_decision_auth
from app.core.config import BACKUP_DIR
from app.core.db import sqlite_connection
from app.core.utils import utc_now
from app.domains.candidates.service import cleaning_registry_map, sync_cleaning_runs
from app.schemas.cleaning import (
    CleaningBatchApprovalRequest,
    CleaningBatchPublishRequest,
    CleaningTaskActionRequest,
    CleaningTaskAdvanceRequest,
)

from .preview_replay import execute_cleaning_replay, generate_cleaning_preview
from .task_queries import cleaning_task_next_action, cleaning_task_replay_ids, cleaning_task_row


def advance_cleaning_task(task_id: str, request: CleaningTaskAdvanceRequest, actor: str = Depends(require_decision_auth)) -> dict[str, Any]:
    """Advance a task once through the common preview/replay/approval/publication state machine."""
    sqlite = sqlite_connection()
    try:
        prior = sqlite.execute(
            "SELECT payload_json FROM audit_event WHERE entity_type='cleaning_task' AND entity_id=? AND event_type='cleaning_task_advanced' ORDER BY event_id DESC LIMIT 100",
            (task_id,),
        ).fetchall()
        for event in prior:
            try:
                payload = json.loads(event["payload_json"])
            except json.JSONDecodeError:
                continue
            if payload.get("idempotencyKey") == request.idempotency_key:
                payload.pop("note", None)
                return payload

        row = cleaning_task_row(sqlite, task_id)
        if not row["is_cleaning"]:
            raise HTTPException(status_code=409, detail="该任务是保留原文策略，不属于清洗任务推进范围")

        action = cleaning_task_next_action(row)
        if action == "completed":
            payload: dict[str, Any] = {
                "taskId": row["cleaning_run_id"],
                "action": "completed",
                "stage": "published",
                "status": "already_completed",
                "targetCount": int(row["candidate_count"]),
                "publishedCount": int(row["published_count"]),
                "idempotencyKey": request.idempotency_key,
                "sourceWrite": False,
                "formalPublication": True,
            }
        elif action == "blocked":
            raise HTTPException(status_code=409, detail=f"任务当前状态不可推进：{row['stage']}")
        elif action == "publication" and not request.confirm_publication:
            payload = {
                "taskId": row["cleaning_run_id"],
                "action": "publication",
                "stage": row["stage"],
                "status": "confirmation_required",
                "targetCount": int(row["candidate_count"]),
                "requiresConfirmation": True,
                "idempotencyKey": request.idempotency_key,
                "sourceWrite": False,
                "formalPublication": False,
            }
        elif action == "preview":
            payload = generate_cleaning_preview(sqlite, row, request.note)
        elif action == "replay":
            payload = execute_cleaning_replay(sqlite, row, request.note)
        elif action == "approval":
            payload = cleaning_batch_approve(
                CleaningBatchApprovalRequest(
                    scope="cleaning",
                    task_id=task_id,
                    note=request.note,
                    idempotency_key=request.idempotency_key,
                ),
                actor=actor,
            )
        elif action == "publication":
            payload = cleaning_publish(
                CleaningBatchPublishRequest(
                    scope="cleaning",
                    task_id=task_id,
                    note=request.note,
                    idempotency_key=request.idempotency_key,
                ),
                actor=actor,
            )
        else:
            raise HTTPException(status_code=409, detail=f"任务没有可执行动作：{action}")

        refreshed = cleaning_task_row(sqlite, task_id)
        payload = {
            **payload,
            "taskId": refreshed["cleaning_run_id"],
            "action": payload.get("action", action),
            "stage": refreshed["stage"],
            "nextAction": cleaning_task_next_action(refreshed),
            "idempotencyKey": request.idempotency_key,
            "sourceWrite": False,
            "formalPublication": bool(payload.get("formalPublication", False)),
        }
        if payload.get("requiresConfirmation"):
            return payload
        now = utc_now()
        sqlite.execute(
            "INSERT INTO audit_event(entity_type,entity_id,event_type,actor,payload_json,event_at) VALUES (?,?,?,?,?,?)",
            ("cleaning_task", task_id, "cleaning_task_advanced", actor, json.dumps(payload, ensure_ascii=False), now),
        )
        sqlite.commit()
        return payload
    except HTTPException:
        if sqlite.in_transaction:
            sqlite.rollback()
        raise
    finally:
        sqlite.close()

def cleaning_batch_approve(request: CleaningBatchApprovalRequest, actor: str = Depends(require_decision_auth)) -> dict[str, Any]:
    """Approve all replay-passed deterministic cleaning rows as one operation."""
    sqlite = sqlite_connection()
    try:
        prior = sqlite.execute(
            "SELECT payload_json FROM audit_event WHERE event_type='cleaning_batch_approval_completed' ORDER BY event_id DESC LIMIT 100"
        ).fetchall()
        for event in prior:
            try:
                payload = json.loads(event["payload_json"])
            except json.JSONDecodeError:
                continue
            if payload.get("idempotencyKey") == request.idempotency_key:
                return payload

        replay_ids = cleaning_task_replay_ids(sqlite, request.task_id)
        if not replay_ids:
            return {"targetCount": 0, "appliedCount": 0, "pendingCount": 0, "completedCount": 0, "status": "empty", "idempotencyKey": request.idempotency_key, "sourceWrite": False, "formalPublication": False}
        if request.task_id:
            task = cleaning_task_row(sqlite, request.task_id)
            if task["stage"] == "published":
                return {"targetCount": int(task["candidate_count"]), "appliedCount": 0, "pendingCount": int(task["pending_count"]), "completedCount": int(task["approved_count"]), "status": "already_completed", "idempotencyKey": request.idempotency_key, "taskId": task["cleaning_run_id"], "sourceWrite": False, "formalPublication": True}
            if task["stage"] not in {"replayed", "previewed"}:
                raise HTTPException(status_code=409, detail="任务尚未通过回放，不能审批")
        marks = ",".join("?" for _ in replay_ids)
        rows = sqlite.execute(
            f"""
            SELECT q.candidate_id,q.proposed_description,c.batch_id,c.validator_status,c.review_state,c.publication_state,
              rr.status AS replay_status,rr.fail_count
            FROM formal_approval_queue q
            JOIN semantic_candidate c ON c.candidate_id=q.candidate_id
            JOIN replay_run rr ON rr.replay_id=q.replay_id
            WHERE q.replay_id IN ({marks}) AND q.status='pending'
            ORDER BY q.replay_id,q.candidate_id
            """,
            replay_ids,
        ).fetchall()
        if not rows:
            completed = sqlite.execute(
                f"SELECT count(*) FROM formal_approval_queue WHERE replay_id IN ({marks}) AND status!='pending'",
                replay_ids,
            ).fetchone()[0]
            return {"targetCount": 0, "appliedCount": 0, "pendingCount": 0, "completedCount": int(completed), "status": "already_completed", "idempotencyKey": request.idempotency_key, "sourceWrite": False, "formalPublication": False}
        invalid = [row["candidate_id"] for row in rows if row["replay_status"] != "passed" or int(row["fail_count"]) != 0 or row["validator_status"] != "candidate" or row["review_state"] != "pending" or row["publication_state"] != "unpublished" or not str(row["proposed_description"] or "").strip()]
        if invalid:
            raise HTTPException(status_code=409, detail=f"清洗批次存在不满足审批门禁的记录：{len(invalid)} 条")

        now = utc_now()
        note = request.note.strip() or "批量清洗确认：仅处理回放通过的确定性规则；审批后仍需独立发布。"
        sqlite.execute("BEGIN IMMEDIATE")
        for row in rows:
            review_id = f"review-{uuid.uuid4().hex}"
            receipt = f"receipt-{uuid.uuid4().hex}"
            sqlite.execute(
                "INSERT INTO review_decision(review_id,candidate_id,decision,reviewed_description,reason_code,review_note,reviewer,approval_receipt,reviewed_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (review_id, row["candidate_id"], "modified", row["proposed_description"], "CLEANING_BATCH_APPROVED", note, actor, receipt, now),
            )
            sqlite.execute("UPDATE semantic_candidate SET review_state='modified' WHERE candidate_id=?", (row["candidate_id"],))
            sqlite.execute("UPDATE formal_approval_queue SET status='modified',note=?,updated_at=? WHERE candidate_id=?", (note, now, row["candidate_id"]))
        for batch_id in sorted({row["batch_id"] for row in rows}):
            sqlite.execute(
                "UPDATE batch_run SET needs_review_count=(SELECT count(*) FROM semantic_candidate WHERE batch_id=? AND review_state='pending'), approved_count=(SELECT count(*) FROM semantic_candidate WHERE batch_id=? AND review_state IN ('approved','modified')) WHERE batch_id=?",
                (batch_id, batch_id, batch_id),
            )
        for replay_id in replay_ids:
            sqlite.execute(
                "UPDATE cleaning_run SET stage='approved',approval_idempotency_key=?,last_error=NULL,updated_at=? WHERE replay_id=?",
                (request.idempotency_key, now, replay_id),
            )
        payload = {"targetCount": len(rows), "appliedCount": len(rows), "pendingCount": 0, "completedCount": len(rows), "status": "completed", "idempotencyKey": request.idempotency_key, "taskId": request.task_id, "sourceWrite": False, "formalPublication": False}
        sqlite.execute(
            "INSERT INTO audit_event(entity_type,entity_id,event_type,actor,payload_json,event_at) VALUES (?,?,?,?,?,?)",
            ("cleaning", "cleaning-batch", "cleaning_batch_approval_completed", actor, json.dumps(payload, ensure_ascii=False), now),
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
        raise HTTPException(status_code=409, detail=f"清洗批量审批写入冲突：{exc}") from exc
    finally:
        sqlite.close()

def approve_cleaning_task(task_id: str, request: CleaningTaskActionRequest, actor: str = Depends(require_decision_auth)) -> dict[str, Any]:
    """Approve one task; the old aggregate endpoint remains a compatibility wrapper."""
    return cleaning_batch_approve(
        CleaningBatchApprovalRequest(
            scope="cleaning",
            task_id=task_id,
            note=request.note,
            idempotency_key=request.idempotency_key,
        ),
        actor=actor,
    )

def cleaning_publish(request: CleaningBatchPublishRequest, actor: str = Depends(require_decision_auth)) -> dict[str, Any]:
    """Publish the already approved cleaning batch into the local formal layer."""
    sqlite = sqlite_connection()
    try:
        replay_ids = cleaning_task_replay_ids(sqlite, request.task_id)
        if not replay_ids:
            return {"targetCount": 0, "publishedCount": 0, "status": "empty", "idempotencyKey": request.idempotency_key, "sourceWrite": False}
        if request.task_id:
            task = cleaning_task_row(sqlite, request.task_id)
            if task["stage"] == "published":
                return {"targetCount": int(task["candidate_count"]), "publishedCount": int(task["published_count"]), "status": "already_completed", "idempotencyKey": request.idempotency_key, "taskId": task["cleaning_run_id"], "backupPath": task["backup_path"] or "", "sourceWrite": False, "formalPublication": True}
            if task["stage"] != "approved":
                raise HTTPException(status_code=409, detail="任务尚未完成审批，不能发布")
        marks = ",".join("?" for _ in replay_ids)
        prior = sqlite.execute(
            "SELECT payload_json FROM audit_event WHERE event_type='cleaning_batch_published' ORDER BY event_id DESC LIMIT 100"
        ).fetchall()
        for event in prior:
            try:
                payload = json.loads(event["payload_json"])
            except json.JSONDecodeError:
                continue
            if payload.get("idempotencyKey") == request.idempotency_key:
                return payload

        rows = sqlite.execute(
            f"""
            SELECT q.candidate_id,q.replay_id,c.batch_id,c.review_state,c.publication_state,
              c.validator_status,c.rule_version,c.validator_version,c.original_description,
              d.source_snapshot_id,d.source_schema,d.site_id,d.asset_number,
              r.review_id,r.reviewed_description,r.approval_receipt
            FROM formal_approval_queue q
            JOIN semantic_candidate c ON c.candidate_id=q.candidate_id
            JOIN device_identity d ON d.device_id=c.device_id
            JOIN review_decision r ON r.candidate_id=q.candidate_id
            WHERE q.replay_id IN ({marks}) AND q.status='modified'
            ORDER BY q.replay_id,q.candidate_id
            """,
            replay_ids,
        ).fetchall()
        if not rows:
            return {"targetCount": 0, "publishedCount": 0, "status": "already_completed", "idempotencyKey": request.idempotency_key, "sourceWrite": False}
        replay_rows = sqlite.execute(
            f"SELECT replay_id,status,evaluation_count,pass_count,fail_count FROM replay_run WHERE replay_id IN ({marks})",
            replay_ids,
        ).fetchall()
        if len(replay_rows) != len(replay_ids) or any(row["status"] != "passed" or int(row["fail_count"]) != 0 or int(row["evaluation_count"]) != int(row["pass_count"]) for row in replay_rows):
            raise HTTPException(status_code=409, detail="发布前校验失败：清洗规则回放未全部通过")
        invalid = [row["candidate_id"] for row in rows if row["review_state"] not in {"modified", "approved"} or row["publication_state"] != "unpublished" or row["validator_status"] != "candidate" or not str(row["reviewed_description"] or "").strip()]
        if invalid:
            raise HTTPException(status_code=409, detail=f"发布前校验失败：{len(invalid)} 条记录状态或描述不符合要求")

        duplicate = sqlite.execute(
            "SELECT candidate_id FROM published_description WHERE candidate_id IN ({})".format(",".join("?" for _ in rows)),
            [row["candidate_id"] for row in rows],
        ).fetchall()
        if duplicate:
            raise HTTPException(status_code=409, detail=f"发布前校验失败：已有 {len(duplicate)} 条正式结果，已停止本批次")

        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        backup_path = BACKUP_DIR / f"semantic_workflow_before_cleaning_publish_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.sqlite3"
        sqlite.execute("VACUUM INTO ?", (str(backup_path),))
        now = utc_now()
        run_id = f"cleaning-publication-{uuid.uuid4().hex}"
        sqlite.execute("BEGIN IMMEDIATE")
        for row in rows:
            final_description = row["reviewed_description"]
            publication_hash = hashlib.sha256(json.dumps({"candidateId": row["candidate_id"], "finalDescription": final_description, "replayId": row["replay_id"], "ruleVersion": row["rule_version"]}, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
            sqlite.execute(
                "INSERT INTO published_description(publication_id,candidate_id,review_id,source_snapshot_id,source_schema,site_id,asset_number,final_description,rule_version,validator_version,replay_id,published_by,published_at,publication_hash) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (f"publication-cleaning-{row['candidate_id']}", row["candidate_id"], row["review_id"], row["source_snapshot_id"], row["source_schema"], row["site_id"], row["asset_number"], final_description, row["rule_version"], row["validator_version"], row["replay_id"], actor, now, publication_hash),
            )
            sqlite.execute("UPDATE semantic_candidate SET publication_state='published',review_state='approved' WHERE candidate_id=?", (row["candidate_id"],))
            sqlite.execute("UPDATE formal_approval_queue SET updated_at=? WHERE candidate_id=?", (now, row["candidate_id"]))
        for batch_id in sorted({row["batch_id"] for row in rows}):
            sqlite.execute("UPDATE batch_run SET published_count=(SELECT count(*) FROM published_description p JOIN semantic_candidate c ON c.candidate_id=p.candidate_id WHERE c.batch_id=?) WHERE batch_id=?", (batch_id, batch_id))
        # Recalculate the task counters from the just-published local results
        # before recording the publication receipt.
        sync_cleaning_runs(sqlite, cleaning_registry_map(sqlite), now)
        for replay_id in replay_ids:
            sqlite.execute(
                """
                UPDATE cleaning_run
                SET stage='published',publication_run_id=?,backup_path=?,source_write=0,formal_publication=1,last_error=NULL,updated_at=?
                WHERE replay_id=?
                """,
                (run_id, str(backup_path), now, replay_id),
            )
        payload = {"publicationRunId": run_id, "targetCount": len(rows), "publishedCount": len(rows), "status": "published", "idempotencyKey": request.idempotency_key, "taskId": request.task_id, "backupPath": str(backup_path), "sourceWrite": False, "formalPublication": True}
        sqlite.execute("INSERT INTO audit_event(entity_type,entity_id,event_type,actor,payload_json,event_at) VALUES (?,?,?,?,?,?)", ("cleaning", run_id, "cleaning_batch_published", actor, json.dumps(payload, ensure_ascii=False), now))
        sqlite.commit()
        return payload
    except HTTPException:
        if sqlite.in_transaction:
            sqlite.rollback()
        raise
    except sqlite3.IntegrityError as exc:
        if sqlite.in_transaction:
            sqlite.rollback()
        raise HTTPException(status_code=409, detail=f"正式发布写入冲突：{exc}") from exc
    finally:
        sqlite.close()

def publish_cleaning_task(task_id: str, request: CleaningTaskActionRequest, actor: str = Depends(require_decision_auth)) -> dict[str, Any]:
    """Publish one approved task; publication remains idempotent and local-only."""
    return cleaning_publish(
        CleaningBatchPublishRequest(
            scope="cleaning",
            task_id=task_id,
            note=request.note,
            idempotency_key=request.idempotency_key,
        ),
        actor=actor,
    )
