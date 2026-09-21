"""Cleaning preview and replay services."""
from __future__ import annotations

import csv
import hashlib
import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from app.core.config import CLEANING_TASK_DIR, PROJECT_ROOT
from app.core.db import sqlite_connection
from app.core.utils import utc_now
from app.schemas.cleaning import CleaningTaskActionRequest, CleaningTaskPreviewRequest

from .task_queries import cleaning_task_row


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
