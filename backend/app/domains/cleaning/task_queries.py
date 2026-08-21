"""Cleaning task query and workflow-state read services."""
from __future__ import annotations

import sqlite3
from typing import Any, Literal

from fastapi import HTTPException, Query

from app.core.db import sqlite_connection
from app.domains.candidates.service import cleaning_registry, cleaning_registry_map, sync_cleaning_runs


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
    from .preview_replay import hydrate_cleaning_preview

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
    from .preview_replay import hydrate_cleaning_preview

    sqlite = sqlite_connection()
    try:
        registry = cleaning_registry_map(sqlite)
        sync_cleaning_runs(sqlite, registry)
        sqlite.commit()
        return {"task": cleaning_task_payload(hydrate_cleaning_preview(sqlite, cleaning_task_row(sqlite, task_id))), "sourceWrite": False}
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
