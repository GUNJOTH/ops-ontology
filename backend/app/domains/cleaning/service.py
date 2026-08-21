"""Cleaning workflow handlers.

This module owns the cleaning task lifecycle previously embedded in app.main.
It only reads/writes the local workflow database and never touches source data.
"""
from __future__ import annotations

import csv
import hashlib
import json
import re
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from app.core.config import (
    BACKUP_DIR,
    CLEANING_TASK_DIR,
    DEPENDENCY_DIR,
    PROJECT_ROOT,
)

if DEPENDENCY_DIR.exists():
    sys.path.insert(0, str(DEPENDENCY_DIR))

from fastapi import Depends, HTTPException, Query

from app.core.auth import require_decision_auth
from app.core.db import sqlite_connection
from app.core.utils import utc_now
from app.domains.candidates.service import (
    cleaning_registry,
    cleaning_registry_map,
    sync_cleaning_runs,
)
from app.schemas.cleaning import (
    CleaningBatchApprovalRequest,
    CleaningBatchPublishRequest,
    CleaningRuleRegistrationRequest,
    CleaningTaskActionRequest,
    CleaningTaskAdvanceRequest,
    CleaningTaskPreviewRequest,
)


def cleaning_task_row(connection: sqlite3.Connection, task_id: str) -> sqlite3.Row:
    row = connection.execute(
        """
        SELECT u.*,r.cleaning_type,r.rule_label,r.action_label,r.is_cleaning,r.rule_version,r.enabled,
          rr.status AS replay_status,rr.evaluation_count,rr.pass_count,rr.fail_count
        FROM cleaning_run u
        JOIN cleaning_rule_registry r ON r.rule_key=u.rule_key AND r.replay_id=u.replay_id
        LEFT JOIN replay_run rr ON rr.replay_id=u.replay_id
            WHERE COALESCE(u.archived,0)=0
              AND (u.cleaning_run_id=? OR u.replay_id=? OR u.rule_key=?)
        LIMIT 1
        """,
        (task_id, task_id, task_id),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"清洗任务不存在：{task_id}")
    return row


def cleaning_task_next_action(row: sqlite3.Row) -> str:
    """Return the only legal next action for a task state."""
    return {
        "task": "preview",
        "previewed": "replay",
        "replayed": "approval",
        "approved": "publication",
        "published": "completed",
        "failed": "replay",
    }.get(str(row["stage"]), "blocked")


def cleaning_task_payload(row: sqlite3.Row) -> dict[str, Any]:
    next_action = cleaning_task_next_action(row)
    return {
        "taskId": row["cleaning_run_id"],
        "ruleKey": row["rule_key"],
        "ruleLabel": row["rule_label"],
        "cleaningType": row["cleaning_type"],
        "actionLabel": row["action_label"],
        "isCleaning": bool(row["is_cleaning"]),
        "replayId": row["replay_id"],
        "ruleVersion": row["rule_version"],
        "stage": row["stage"],
        "nextAction": next_action,
        "availableActions": [] if next_action in {"completed", "blocked"} else [next_action],
        "status": row["status"],
        "candidateCount": int(row["candidate_count"]),
        "pendingCount": int(row["pending_count"]),
        "approvedCount": int(row["approved_count"]),
        "publishedCount": int(row["published_count"]),
        "previewRows": int(row["preview_rows"] or 0),
        "replayRows": int(row["replay_rows"] or 0),
        "replayStatus": row["replay_status"],
        "replayEvaluationCount": int(row["evaluation_count"] or 0),
        "replayPassCount": int(row["pass_count"] or 0),
        "replayFailCount": int(row["fail_count"] or 0),
        "previewId": row["preview_id"],
        "previewSha256": row["preview_sha256"],
        "previewPath": row["preview_path"],
        "samplePath": row["sample_path"],
        "approvalIdempotencyKey": row["approval_idempotency_key"],
        "publicationRunId": row["publication_run_id"],
        "backupPath": row["backup_path"],
        "sourceWrite": bool(row["source_write"]),
        "formalPublication": bool(row["formal_publication"]),
        "lastError": row["last_error"],
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }


def hydrate_cleaning_preview(connection: sqlite3.Connection, row: sqlite3.Row) -> sqlite3.Row:
    """Backfill preview provenance from the existing immutable pilot artifacts once."""
    if row["preview_id"] and row["preview_sha256"]:
        preview_rows = int(row["preview_rows"] or 0)
        replay_rows = int(row["replay_rows"] or 0)
        preview_path = Path(row["preview_path"]) if row["preview_path"] else None
        if preview_rows == 0 and preview_path and preview_path.exists():
            with preview_path.open("r", encoding="utf-8-sig", newline="") as handle:
                preview_rows = sum(1 for _ in csv.DictReader(handle))
        if replay_rows == 0 and row["evaluation_count"]:
            replay_rows = int(row["evaluation_count"])
        if preview_rows != int(row["preview_rows"] or 0) or replay_rows != int(row["replay_rows"] or 0):
            connection.execute(
                "UPDATE cleaning_run SET preview_rows=?,replay_rows=?,updated_at=? WHERE cleaning_run_id=?",
                (preview_rows, replay_rows, utc_now(), row["cleaning_run_id"]),
            )
            connection.commit()
            return cleaning_task_row(connection, row["cleaning_run_id"])
        return row
    candidates = [
        CLEANING_TASK_DIR / row["rule_key"],
        PROJECT_ROOT / "pilots" / "HD_SAAS" / "question_separator_preview" if row["rule_key"] == "semantic.separator.fullwidth_question_mark_to_space" else None,
        PROJECT_ROOT / "pilots" / "HD_SAAS" / "terminal_hyphen_preview" if row["rule_key"] == "format.terminal_hyphen_trim" else None,
        PROJECT_ROOT / "pilots" / "HD_SAAS" / "ai_cluster_keep_original_replay" if row["rule_key"] == "policy.keep_original.leading_minus" else None,
    ]
    for directory in candidates:
        if directory is None:
            continue
        manifest = directory / "manifest.json"
        replay_manifest = directory / "replay_manifest.json"
        if not manifest.exists() and not replay_manifest.exists():
            continue
        payload: dict[str, Any] = {}
        for path in (manifest, replay_manifest):
            if path.exists():
                try:
                    payload.update(json.loads(path.read_text(encoding="utf-8")))
                except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                    continue
        preview_id = payload.get("preview_id") or payload.get("replay_id")
        preview_sha = payload.get("preview_sha256") or payload.get("replay_sha256")
        preview_file = payload.get("preview_file")
        sample_file = payload.get("sample_file")
        preview_count = payload.get("preview_rows") or payload.get("evaluation_count") or 0
        replay_count = payload.get("evaluation_count") or payload.get("preview_rows") or 0
        if preview_id and preview_sha:
            connection.execute(
                "UPDATE cleaning_run SET preview_id=?,preview_sha256=?,preview_path=?,sample_path=?,preview_rows=?,replay_rows=?,updated_at=? WHERE cleaning_run_id=?",
                (str(preview_id), str(preview_sha), preview_file, sample_file, int(preview_count), int(replay_count), utc_now(), row["cleaning_run_id"]),
            )
            connection.commit()
            return cleaning_task_row(connection, row["cleaning_run_id"])
    return row


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def generate_cleaning_preview(connection: sqlite3.Connection, row: sqlite3.Row, note: str = "") -> dict[str, Any]:
    """Build a task-scoped preview from the queued candidate rows only.

    This is deliberately read-only with respect to source and candidate content;
    it only records immutable preview provenance on cleaning_run.
    """
    queue_rows = connection.execute(
        """
        SELECT q.queue_id,q.candidate_id,q.proposed_description,q.status,
          c.original_description,c.candidate_description,c.review_state,c.publication_state,
          d.site_id,d.asset_number,d.source_asset_id,d.location_code,d.location_description,
          d.location_parent,d.classification_description
        FROM formal_approval_queue q
        JOIN semantic_candidate c ON c.candidate_id=q.candidate_id
        JOIN device_identity d ON d.device_id=c.device_id
        WHERE q.replay_id=?
        ORDER BY d.site_id,d.asset_number,q.candidate_id
        """,
        (row["replay_id"],),
    ).fetchall()
    if not queue_rows:
        raise HTTPException(status_code=409, detail="任务没有可生成预览的候选记录")

    output_dir = CLEANING_TASK_DIR / re.sub(r"[^A-Za-z0-9_.-]+", "_", row["rule_key"])
    output_dir.mkdir(parents=True, exist_ok=True)
    preview_id = f"preview-{row['cleaning_run_id']}-{hashlib.sha256('|'.join(item['candidate_id'] for item in queue_rows).encode('utf-8')).hexdigest()[:16]}"
    preview_path = output_dir / "preview.csv"
    sample_path = output_dir / "sample.csv"
    fieldnames = ["TASK_ID", "RULE_KEY", "RULE_VERSION", "QUEUE_ID", "CANDIDATE_ID", "SITEID", "ASSETNUM", "ASSETID", "ORIGINAL_DESCRIPTION", "CANDIDATE_DESCRIPTION", "PROPOSED_DESCRIPTION", "STATUS", "REVIEW_STATE", "PUBLICATION_STATE", "LOCATION_CODE", "LOCATION_DESCRIPTION", "LOCATION_PARENT", "CLASSIFICATION_DESCRIPTION"]

    def as_dict(item: sqlite3.Row) -> dict[str, Any]:
        return {
            "TASK_ID": row["cleaning_run_id"],
            "RULE_KEY": row["rule_key"],
            "RULE_VERSION": row["rule_version"],
            "QUEUE_ID": item["queue_id"],
            "CANDIDATE_ID": item["candidate_id"],
            "SITEID": item["site_id"] or "",
            "ASSETNUM": item["asset_number"] or "",
            "ASSETID": item["source_asset_id"] or "",
            "ORIGINAL_DESCRIPTION": item["original_description"] or "",
            "CANDIDATE_DESCRIPTION": item["candidate_description"] or "",
            "PROPOSED_DESCRIPTION": item["proposed_description"] or "",
            "STATUS": item["status"],
            "REVIEW_STATE": item["review_state"],
            "PUBLICATION_STATE": item["publication_state"],
            "LOCATION_CODE": item["location_code"] or "",
            "LOCATION_DESCRIPTION": item["location_description"] or "",
            "LOCATION_PARENT": item["location_parent"] or "",
            "CLASSIFICATION_DESCRIPTION": item["classification_description"] or "",
        }

    preview_rows = [as_dict(item) for item in queue_rows]
    for path, items in ((preview_path, preview_rows), (sample_path, preview_rows[: min(200, len(preview_rows))])):
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(items)
    now = utc_now()
    preview_sha256 = sha256_file(preview_path)
    connection.execute(
        """
        UPDATE cleaning_run
        SET stage=CASE WHEN stage='published' THEN stage ELSE 'previewed' END,
            preview_id=?,preview_sha256=?,preview_path=?,sample_path=?,preview_rows=?,last_error=NULL,updated_at=?
        WHERE cleaning_run_id=?
        """,
        (preview_id, preview_sha256, str(preview_path), str(sample_path), len(preview_rows), now, row["cleaning_run_id"]),
    )
    payload = {"taskId": row["cleaning_run_id"], "stage": row["stage"] if row["stage"] == "published" else "previewed", "previewId": preview_id, "previewSha256": preview_sha256, "previewRows": len(preview_rows), "sampleRows": min(200, len(preview_rows)), "previewPath": str(preview_path), "samplePath": str(sample_path), "sourceWrite": False, "formalPublication": False}
    connection.execute(
        "INSERT INTO audit_event(entity_type,entity_id,event_type,actor,payload_json,event_at) VALUES (?,?,?,?,?,?)",
        ("cleaning_task", row["cleaning_run_id"], "cleaning_preview_generated", "local-user", json.dumps({**payload, "note": note}, ensure_ascii=False), now),
    )
    connection.commit()
    return payload


def expected_cleaning_description(cleaning_type: str, original: str, proposed: str) -> str:
    if cleaning_type == "separator":
        return original.replace("？", " ")
    if cleaning_type == "terminal_hyphen":
        return original[:-1] if original.endswith("-") else original
    if cleaning_type == "keep_original":
        return original
    if cleaning_type in {"space", "whitespace"}:
        return re.sub(r"[\s\u3000]+", " ", original).strip()
    return proposed


def execute_cleaning_replay(connection: sqlite3.Connection, row: sqlite3.Row, note: str = "") -> dict[str, Any]:
    row = hydrate_cleaning_preview(connection, row)
    if not row["preview_path"] or not Path(row["preview_path"]).exists():
        generate_cleaning_preview(connection, row, note)
        row = cleaning_task_row(connection, row["cleaning_run_id"])
    preview_path = Path(row["preview_path"])
    with preview_path.open("r", encoding="utf-8-sig", newline="") as handle:
        preview_rows = list(csv.DictReader(handle))
    if not preview_rows:
        raise HTTPException(status_code=409, detail="预览为空，不能回放")
    ids = [item.get("CANDIDATE_ID", "") for item in preview_rows]
    if any(not item for item in ids) or len(set(ids)) != len(ids):
        raise HTTPException(status_code=409, detail="预览存在空的或重复的候选身份")
    placeholders = ",".join("?" for _ in ids)
    db_rows = connection.execute(
        f"""
        SELECT q.candidate_id,q.proposed_decision,q.proposed_description,
          c.original_description,c.candidate_description,c.review_state,c.publication_state,
          d.source_snapshot_id,d.source_schema,d.site_id,d.asset_number,d.location_code,
          d.location_description,d.location_parent,d.classification_description
        FROM formal_approval_queue q
        JOIN semantic_candidate c ON c.candidate_id=q.candidate_id
        JOIN device_identity d ON d.device_id=c.device_id
        WHERE q.replay_id=? AND q.candidate_id IN ({placeholders})
        """,
        (row["replay_id"], *ids),
    ).fetchall()
    by_id = {item["candidate_id"]: item for item in db_rows}
    failures: list[dict[str, str]] = []
    results: list[tuple[str, sqlite3.Row, dict[str, str], str]] = []
    for item in preview_rows:
        candidate_id = item["CANDIDATE_ID"]
        db_row = by_id.get(candidate_id)
        if db_row is None:
            failures.append({"candidateId": candidate_id, "reason": "candidate_missing_or_wrong_task"})
            continue
        checks = {
            "original_matches": item.get("ORIGINAL_DESCRIPTION", "") == (db_row["original_description"] or ""),
            "proposed_matches": item.get("PROPOSED_DESCRIPTION", "") == (db_row["proposed_description"] or ""),
            "identity_matches": item.get("SITEID", "") == (db_row["site_id"] or "") and item.get("ASSETNUM", "") == (db_row["asset_number"] or ""),
            "description_nonempty": bool(str(item.get("PROPOSED_DESCRIPTION", "")).strip()),
            "rule_transformation_matches": item.get("PROPOSED_DESCRIPTION", "") == expected_cleaning_description(row["cleaning_type"], db_row["original_description"] or "", db_row["proposed_description"] or ""),
        }
        for check, passed in checks.items():
            if not passed:
                failures.append({"candidateId": candidate_id, "reason": check})
        results.append((candidate_id, db_row, item, "modified" if row["is_cleaning"] else "approved"))
    if len(db_rows) != len(preview_rows):
        failures.append({"candidateId": "(scope)", "reason": "preview_db_count_mismatch"})
    replay_id = row["replay_id"]
    now = utc_now()
    existing = connection.execute("SELECT * FROM replay_run WHERE replay_id=?", (replay_id,)).fetchone()
    passed = len(results) - sum(1 for item in failures if item["candidateId"] != "(scope)")
    failed = len(preview_rows) - passed
    if any(item["candidateId"] == "(scope)" for item in failures):
        failed = max(failed, 1)
    status = "passed" if not failures else "failed"
    if existing is None:
        connection.execute(
            "INSERT INTO replay_run(replay_id,rule_version,validator_version,evaluation_count,pass_count,fail_count,status,started_at,finished_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (replay_id, row["rule_version"], "cleaning-task-replay-v1", len(preview_rows), passed, failed, status, now, utc_now()),
        )
        for candidate_id, db_row, item, decision in results:
            case_id = f"case-{replay_id}-{candidate_id}"
            connection.execute(
                "INSERT OR IGNORE INTO evaluation_case(case_id,source_review_id,source_snapshot_id,source_schema,site_id,asset_number,input_description,context_json,expected_decision,expected_description,failure_type,active,introduced_rule_version,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (case_id, None, db_row["source_snapshot_id"], db_row["source_schema"], db_row["site_id"], db_row["asset_number"], db_row["original_description"], json.dumps({"task_id": row["cleaning_run_id"], "rule_key": row["rule_key"], "source_write": False, "formal_publication": False}, ensure_ascii=False), decision, item["PROPOSED_DESCRIPTION"], row["cleaning_type"], 1, row["rule_version"], now),
            )
            connection.execute(
                "INSERT INTO replay_result(replay_id,case_id,actual_decision,actual_description,outcome,message) VALUES (?,?,?,?,?,?)",
                (replay_id, case_id, decision, item["PROPOSED_DESCRIPTION"], "pass" if not any(f["candidateId"] == candidate_id for f in failures) else "fail", None),
            )
        connection.execute(
            "INSERT INTO audit_event(entity_type,entity_id,event_type,actor,payload_json,event_at) VALUES (?,?,?,?,?,?)",
            ("cleaning_task", row["cleaning_run_id"], "cleaning_replay_executed", "local-user", json.dumps({"replayId": replay_id, "previewId": row["preview_id"], "evaluationCount": len(preview_rows), "passCount": passed, "failCount": failed, "sourceWrite": False, "formalPublication": False, "note": note}, ensure_ascii=False), now),
        )
    else:
        status = existing["status"]
        passed = int(existing["pass_count"])
        failed = int(existing["fail_count"])
    connection.execute("UPDATE cleaning_run SET stage=CASE WHEN stage='published' THEN stage ELSE ? END,replay_rows=?,last_error=?,updated_at=? WHERE cleaning_run_id=?", ("replayed" if status == "passed" else "failed", len(preview_rows), None if status == "passed" else "回放校验失败", now, row["cleaning_run_id"]))
    connection.commit()
    return {"taskId": row["cleaning_run_id"], "stage": "published" if row["stage"] == "published" else ("replayed" if status == "passed" else "failed"), "replayId": replay_id, "evaluationCount": len(preview_rows), "passCount": passed, "failCount": failed, "status": status, "failures": failures, "sourceWrite": False, "formalPublication": False}


def cleaning_task_replay_ids(connection: sqlite3.Connection, task_id: str | None) -> tuple[str, ...]:
    registry = cleaning_registry(connection)
    if not task_id:
        return tuple(row["replay_id"] for row in registry if row["is_cleaning"])
    row = cleaning_task_row(connection, task_id)
    if not row["is_cleaning"]:
        raise HTTPException(status_code=409, detail="该任务是保留原文策略，不属于清洗发布范围")
    return (row["replay_id"],)


def cleaning_tasks() -> dict[str, Any]:
    """Return the single task model used by preview, replay, approval and publication."""
    sqlite = sqlite_connection()
    try:
        registry = cleaning_registry_map(sqlite)
        sync_cleaning_runs(sqlite, registry)
        sqlite.commit()
        rows = sqlite.execute(
            """
            SELECT u.*,r.cleaning_type,r.rule_label,r.action_label,r.is_cleaning,r.rule_version,r.enabled,
              rr.status AS replay_status,rr.evaluation_count,rr.pass_count,rr.fail_count
            FROM cleaning_run u
            JOIN cleaning_rule_registry r ON r.rule_key=u.rule_key AND r.replay_id=u.replay_id
            LEFT JOIN replay_run rr ON rr.replay_id=u.replay_id
            WHERE r.enabled=1
            ORDER BY u.created_at,u.cleaning_run_id
            """
        ).fetchall()
        rows = [hydrate_cleaning_preview(sqlite, row) for row in rows]
        return {
            "tasks": [cleaning_task_payload(row) for row in rows],
            "workflow": ["preview", "replay", "approval", "publication"],
            "sourceWrite": False,
        }
    finally:
        sqlite.close()


def cleaning_task(task_id: str) -> dict[str, Any]:
    sqlite = sqlite_connection()
    try:
        registry = cleaning_registry_map(sqlite)
        sync_cleaning_runs(sqlite, registry)
        sqlite.commit()
        return {"task": cleaning_task_payload(hydrate_cleaning_preview(sqlite, cleaning_task_row(sqlite, task_id))), "sourceWrite": False}
    finally:
        sqlite.close()


def record_cleaning_preview(task_id: str, request: CleaningTaskPreviewRequest) -> dict[str, Any]:
    """Register a generated preview without changing candidate or source rows."""
    sqlite = sqlite_connection()
    try:
        row = cleaning_task_row(sqlite, task_id)
        if row["stage"] == "published":
            raise HTTPException(status_code=409, detail="已发布任务不能重新生成预览")
        if not request.preview_id or not request.preview_sha256:
            return generate_cleaning_preview(sqlite, row, request.note)
        now = utc_now()
        sqlite.execute(
            """
            UPDATE cleaning_run
            SET stage='previewed',preview_id=?,preview_sha256=?,preview_path=?,sample_path=?,last_error=NULL,updated_at=?
            WHERE cleaning_run_id=?
            """,
            (request.preview_id, request.preview_sha256, request.preview_path, request.sample_path, now, row["cleaning_run_id"]),
        )
        payload = {"taskId": row["cleaning_run_id"], "stage": "previewed", "previewId": request.preview_id, "previewSha256": request.preview_sha256, "sourceWrite": False}
        sqlite.execute(
            "INSERT INTO audit_event(entity_type,entity_id,event_type,actor,payload_json,event_at) VALUES (?,?,?,?,?,?)",
            ("cleaning_task", row["cleaning_run_id"], "cleaning_preview_recorded", "local-user", json.dumps({**payload, "note": request.note}, ensure_ascii=False), now),
        )
        sqlite.commit()
        return payload
    finally:
        sqlite.close()


def gate_cleaning_replay(task_id: str, request: CleaningTaskActionRequest) -> dict[str, Any]:
    """Run the task-scoped deterministic replay and open the approval gate."""
    sqlite = sqlite_connection()
    try:
        row = cleaning_task_row(sqlite, task_id)
        payload = execute_cleaning_replay(sqlite, row, request.note)
        payload["idempotencyKey"] = request.idempotency_key
        return payload
    finally:
        sqlite.close()


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


def cleaning_rules() -> dict[str, Any]:
    sqlite = sqlite_connection()
    try:
        registry = cleaning_registry(sqlite)
        sync_cleaning_runs(sqlite, {row["replay_id"]: dict(row) for row in registry})
        sqlite.commit()
        runs = sqlite.execute(
            """
            SELECT r.rule_key,r.cleaning_type,r.rule_label,r.action_label,r.is_cleaning,r.replay_id,r.rule_version,r.enabled,
              u.cleaning_run_id,u.status,u.stage,u.candidate_count,u.pending_count,u.approved_count,u.published_count,
              u.preview_id,u.preview_sha256,u.preview_path,u.sample_path,u.approval_idempotency_key,u.publication_run_id,u.backup_path,
               u.source_write,u.formal_publication,u.archived,u.archived_at,u.archive_reason,u.last_error,u.updated_at
             FROM cleaning_rule_registry r JOIN cleaning_run u ON u.rule_key=r.rule_key AND u.replay_id=r.replay_id
             WHERE r.enabled=1 AND COALESCE(u.archived,0)=0 ORDER BY r.is_cleaning DESC,r.rule_key
            """
        ).fetchall()
        return {"rules": [dict(row) for row in runs]}
    finally:
        sqlite.close()


def register_cleaning_rule(request: CleaningRuleRegistrationRequest, actor: str = Depends(require_decision_auth)) -> dict[str, Any]:
    sqlite = sqlite_connection()
    try:
        now = utc_now()
        sqlite.execute(
            "INSERT INTO cleaning_rule_registry(rule_key,cleaning_type,rule_label,action_label,is_cleaning,replay_id,rule_version,enabled,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (request.rule_key, request.cleaning_type, request.rule_label, request.action_label, int(request.is_cleaning), request.replay_id, request.rule_version, 0, now, now),
        )
        sqlite.execute(
            "INSERT INTO cleaning_run(cleaning_run_id,rule_key,replay_id,status,created_at,updated_at) VALUES (?,?,?,?,?,?)",
            (f"cleaning-run-{request.replay_id}", request.rule_key, request.replay_id, "draft", now, now),
        )
        sqlite.execute(
            "INSERT INTO audit_event(entity_type,entity_id,event_type,actor,payload_json,event_at) VALUES (?,?,?,?,?,?)",
            ("cleaning_rule", request.rule_key, "cleaning_rule_registered", actor, json.dumps(request.model_dump(by_alias=True), ensure_ascii=False), now),
        )
        sqlite.commit()
        return {"ruleKey": request.rule_key, "replayId": request.replay_id, "status": "registered"}
    except sqlite3.IntegrityError as exc:
        sqlite.rollback()
        raise HTTPException(status_code=409, detail=f"清洗规则已存在或 replay_id 冲突：{exc}") from exc
    finally:
        sqlite.close()


def cleaning_queue(
    scope: Literal["cleaning", "keep_original", "all"] = "cleaning",
    status: Literal["all", "pending", "approved", "modified", "rejected", "deferred"] = "pending",
    task_id: str | None = Query(default=None, alias="task_id"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    """One compact workbench for deterministic cleaning batches."""
    sqlite = sqlite_connection()
    try:
        registry = cleaning_registry_map(sqlite)
        replay_ids = cleaning_task_replay_ids(sqlite, task_id) if task_id else tuple(registry)
        if not replay_ids:
            return {"rows": [], "total": 0, "page": page, "pageSize": page_size, "summary": {"totalPending": 0, "cleaningPending": 0, "cleaningPublished": 0, "keepOriginalPending": 0, "rules": []}}
        sync_registry = registry if not task_id else {replay_id: registry[replay_id] for replay_id in replay_ids if replay_id in registry}
        sync_cleaning_runs(sqlite, sync_registry)
        sqlite.commit()
        scoped_replay_ids = tuple(
            replay_id
            for replay_id in replay_ids
            if (scope == "all"
                or (scope == "cleaning" and registry.get(replay_id, {}).get("is_cleaning"))
                or (scope == "keep_original" and not registry.get(replay_id, {}).get("is_cleaning")))
        )
        if not scoped_replay_ids:
            return {"rows": [], "total": 0, "page": page, "pageSize": page_size, "summary": {"totalPending": 0, "cleaningPending": 0, "cleaningPublished": 0, "keepOriginalPending": 0, "rules": []}}
        replay_marks = ",".join("?" for _ in scoped_replay_ids)
        where_parts = [
            f"q.replay_id IN ({replay_marks})",
            "EXISTS (SELECT 1 FROM cleaning_run u WHERE u.replay_id=q.replay_id AND COALESCE(u.archived,0)=0)",
        ]
        where_parameters: list[Any] = list(scoped_replay_ids)
        if status != "all":
            where_parts.append("q.status=?")
            where_parameters.append(status)
        where_sql = " AND ".join(where_parts)
        total = int(sqlite.execute(f"SELECT count(*) FROM formal_approval_queue q WHERE {where_sql}", where_parameters).fetchone()[0])
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
            WHERE {where_sql}
            ORDER BY q.created_at,q.queue_id
            LIMIT ? OFFSET ?
            """,
            where_parameters + [page_size, (page - 1) * page_size],
        ).fetchall()
        mapped: list[dict[str, Any]] = []
        for row in rows:
            meta = registry.get(row["replay_id"], {"cleaning_type": "other", "rule_key": "other", "rule_label": "其他待确认", "action_label": "待确认", "is_cleaning": 0})
            mapped.append({
                "queueId": row["queue_id"],
                "candidateId": row["candidate_id"],
                "clusterId": row["cluster_id"],
                "replayId": row["replay_id"],
                "cleaningType": meta["cleaning_type"],
                "ruleKey": meta["rule_key"],
                "ruleLabel": meta["rule_label"],
                "actionLabel": meta["action_label"],
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
        summary_rows = sqlite.execute(
            f"""
            SELECT q.replay_id,
              COUNT(*) total,
              SUM(CASE WHEN q.status='pending' THEN 1 ELSE 0 END) pending,
              SUM(CASE WHEN q.status!='pending' THEN 1 ELSE 0 END) completed,
              SUM(CASE WHEN c.publication_state='published' THEN 1 ELSE 0 END) published
            FROM formal_approval_queue q
            JOIN semantic_candidate c ON c.candidate_id=q.candidate_id
             WHERE q.replay_id IN ({replay_marks})
               AND EXISTS (SELECT 1 FROM cleaning_run u WHERE u.replay_id=q.replay_id AND COALESCE(u.archived,0)=0)
            GROUP BY q.replay_id
            """,
            scoped_replay_ids,
        ).fetchall()
        total_pending = sum(int(row["pending"] or 0) for row in summary_rows)
        cleaning_pending = sum(int(row["pending"] or 0) for row in summary_rows if registry.get(row["replay_id"], {}).get("is_cleaning"))
        keep_original_pending = total_pending - cleaning_pending
        cleaning_published = sum(int(row["published"] or 0) for row in summary_rows if registry.get(row["replay_id"], {}).get("is_cleaning"))
        by_rule: dict[str, dict[str, Any]] = {}
        for row in summary_rows:
            meta = registry.get(row["replay_id"], {"rule_key": "other", "rule_label": "其他待确认", "is_cleaning": 0})
            key = str(meta["rule_key"])
            item = by_rule.setdefault(key, {"ruleKey": key, "ruleLabel": meta["rule_label"], "total": 0, "pending": 0, "completed": 0, "cleaning": bool(meta["is_cleaning"])})
            item["total"] += int(row["total"] or 0)
            item["pending"] += int(row["pending"] or 0)
            item["completed"] += int(row["completed"] or 0)
        return {
            "rows": mapped,
            "total": total,
            "page": page,
            "pageSize": page_size,
            "summary": {
                "totalPending": total_pending,
                "cleaningPending": cleaning_pending,
                "cleaningPublished": cleaning_published,
                "keepOriginalPending": keep_original_pending,
                "rules": list(by_rule.values()),
            },
        }
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
