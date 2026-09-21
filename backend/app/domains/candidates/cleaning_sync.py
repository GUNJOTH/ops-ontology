"""Cleaning registry helpers shared by cleaning and approval flows.

These helpers are intentionally small and keep the cleaning task state aligned
with the formal approval queue.
"""
from __future__ import annotations

import sqlite3
from typing import Any

from app.core.utils import utc_now


def cleaning_registry(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    return connection.execute(
        "SELECT rule_key,cleaning_type,rule_label,action_label,is_cleaning,replay_id,rule_version,enabled FROM cleaning_rule_registry WHERE enabled=1 ORDER BY is_cleaning DESC,rule_key"
    ).fetchall()


def cleaning_registry_map(connection: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    return {row["replay_id"]: dict(row) for row in cleaning_registry(connection)}


def sync_cleaning_runs(connection: sqlite3.Connection, registry: dict[str, dict[str, Any]], now: str | None = None) -> None:
    now = now or utc_now()
    if not registry:
        return
    replay_ids = tuple(registry)
    marks = ",".join("?" for _ in replay_ids)
    counts_by_replay = {
        row["replay_id"]: row
        for row in connection.execute(
            f"""
            SELECT q.replay_id,
              COUNT(*) total,
              SUM(CASE WHEN q.status='pending' THEN 1 ELSE 0 END) pending,
              SUM(CASE WHEN q.status!='pending' THEN 1 ELSE 0 END) approved,
              SUM(CASE WHEN c.publication_state='published' THEN 1 ELSE 0 END) published,
              MIN(c.batch_id) batch_id
            FROM formal_approval_queue q
            JOIN semantic_candidate c ON c.candidate_id=q.candidate_id
            WHERE q.replay_id IN ({marks})
            GROUP BY q.replay_id
            """,
            replay_ids,
        ).fetchall()
    }
    preview_by_replay = {
        row["replay_id"]: row
        for row in connection.execute(
            f"SELECT replay_id,preview_rows FROM cleaning_run WHERE replay_id IN ({marks})",
            replay_ids,
        ).fetchall()
    }
    replay_by_id = {
        row["replay_id"]: row
        for row in connection.execute(
            f"SELECT replay_id,status,fail_count FROM replay_run WHERE replay_id IN ({marks})",
            replay_ids,
        ).fetchall()
    }
    existing_by_replay = {
        row["replay_id"]: row
        for row in connection.execute(
            f"SELECT replay_id,status,stage,source_type FROM cleaning_run WHERE replay_id IN ({marks})",
            replay_ids,
        ).fetchall()
    }
    for replay_id, meta in registry.items():
        counts = counts_by_replay.get(replay_id)
        if counts is None:
            counts = {"total": 0, "pending": 0, "approved": 0, "published": 0, "batch_id": None}
        pending = int(counts["pending"] or 0)
        approved = int(counts["approved"] or 0)
        published = int(counts["published"] or 0)
        total = int(counts["total"] or 0)
        task_row = preview_by_replay.get(replay_id)
        preview_rows = int(task_row["preview_rows"] or 0) if task_row else 0
        replay = replay_by_id.get(replay_id)
        replay_status = replay["status"] if replay else None
        replay_fail_count = int(replay["fail_count"] or 0) if replay else 1
        existing_task = existing_by_replay.get(replay_id)
        is_approved_agent_task = bool(
            existing_task
            and existing_task["source_type"] == "rule_agent"
            and existing_task["stage"] in {"approved", "published"}
            and replay_status == "passed"
            and replay_fail_count == 0
        )
        status = "published" if total and published == total else "approved" if approved and pending == 0 else "pending_approval" if total else (existing_task["status"] if is_approved_agent_task else "draft")
        stage = (
            "published" if total and published == total else
            "approved" if approved and pending == 0 else
            "replayed" if replay_status == "passed" and replay_fail_count == 0 else
            "previewed" if total else
            (existing_task["stage"] if is_approved_agent_task else "task")
        )
        connection.execute(
            """
            UPDATE cleaning_run
            SET batch_id=?,status=?,stage=?,candidate_count=?,pending_count=?,approved_count=?,published_count=?,
                formal_publication=?,source_write=0,updated_at=?
            WHERE replay_id=?
            """,
            (
                counts["batch_id"],
                status,
                stage,
                total or preview_rows,
                pending,
                approved,
                published,
                1 if total and published == total else 0,
                now,
                replay_id,
            ),
        )
