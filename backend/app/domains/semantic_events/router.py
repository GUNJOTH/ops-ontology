"""Semantic event query routes (native APIRouter)."""
from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.core.db import unified_semantics_connection


def semantic_events_summary() -> dict[str, Any]:
    connection = unified_semantics_connection()
    try:
        latest = connection.execute(
            "SELECT * FROM semantic_event_layer_run ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        if latest is None:
            raise HTTPException(status_code=503, detail="统一事件层尚未构建")
        by_type = [dict(row) for row in connection.execute(
            "SELECT event_type AS value,count(*) AS count FROM semantic_event WHERE status IN ('observed','accepted') GROUP BY event_type ORDER BY event_type"
        ).fetchall()]
        by_source = [dict(row) for row in connection.execute(
            "SELECT source_schema AS value,count(*) AS count FROM semantic_event WHERE status IN ('observed','accepted') GROUP BY source_schema ORDER BY source_schema"
        ).fetchall()]
        return {
            "latestRun": dict(latest),
            "eventCount": int(connection.execute("SELECT count(*) FROM semantic_event WHERE status IN ('observed','accepted')").fetchone()[0]),
            "reviewEventCount": int(connection.execute("SELECT count(*) FROM semantic_event WHERE status='needs_review'").fetchone()[0]),
            "eventTypes": by_type,
            "sources": by_source,
            "sourceWrite": False,
            "formalPublication": False,
            "policy": [
                "只接收已有设备身份桥接且来源可追溯的事件事实",
                "保留源事件类型、源状态、源描述和发生时间",
                "事件层不替代事实层，也不直接产生行动",
            ],
        }
    finally:
        connection.close()


def semantic_events(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    event_type: str = "all",
    status: str = "all",
    site_id: str | None = None,
    search: str | None = None,
) -> dict[str, Any]:
    connection = unified_semantics_connection()
    try:
        where: list[str] = []
        parameters: list[Any] = []
        if event_type and event_type != "all":
            where.append("e.event_type=?")
            parameters.append(event_type)
        if status and status != "all":
            where.append("e.status=?")
            parameters.append(status)
        if site_id and site_id.strip():
            where.append("e.site_id=?")
            parameters.append(site_id.strip())
        if search and search.strip():
            value = f"%{search.strip()}%"
            where.append("(e.event_id LIKE ? OR e.subject_key LIKE ? OR e.source_row_id LIKE ? OR e.location_code LIKE ? OR e.raw_status LIKE ? OR e.description LIKE ?)")
            parameters.extend([value] * 6)
        where_sql = f"WHERE {' AND '.join(where)}" if where else ""
        total = int(connection.execute(f"SELECT count(*) FROM semantic_event e {where_sql}", parameters).fetchone()[0])
        rows = connection.execute(
            f"""
            SELECT e.*,s.current_state,s.display_name AS current_state_display
            FROM semantic_event e
            LEFT JOIN semantic_current_state s
              ON s.subject_type=e.subject_type AND s.subject_key=e.subject_key
             AND s.state_domain='DEFECT' AND s.status='current'
            {where_sql}
            ORDER BY e.occurred_at DESC,e.event_id
            LIMIT ? OFFSET ?
            """,
            parameters + [page_size, (page - 1) * page_size],
        ).fetchall()
        return {
            "items": [dict(row) for row in rows],
            "total": total,
            "page": page,
            "pageSize": page_size,
            "sourceWrite": False,
            "formalPublication": False,
        }
    finally:
        connection.close()


def semantic_event_detail(event_id: str) -> dict[str, Any]:
    connection = unified_semantics_connection()
    try:
        event = connection.execute("SELECT * FROM semantic_event WHERE event_id=?", (event_id,)).fetchone()
        if event is None:
            raise HTTPException(status_code=404, detail="统一事件不存在")
        item = dict(event)
        try:
            item["payload"] = json.loads(item.get("payload_json") or "{}")
        except json.JSONDecodeError:
            item["payload"] = {}
        source_facts = [dict(row) for row in connection.execute(
            """
            SELECT * FROM semantic_fact
            WHERE source_schema=? AND source_table=? AND source_row_id=? AND source_snapshot_id=?
            ORDER BY created_at DESC
            """,
            (event["source_schema"], event["source_table"], event["source_row_id"], event["source_snapshot_id"]),
        ).fetchall()]
        transitions = [dict(row) for row in connection.execute(
            "SELECT * FROM semantic_state_transition WHERE event_id=? ORDER BY created_at DESC",
            (event_id,),
        ).fetchall()]
        current = connection.execute(
            """
            SELECT * FROM semantic_current_state
            WHERE subject_type=? AND subject_key=? AND state_domain='DEFECT' AND status='current'
            """,
            (event["subject_type"], event["subject_key"]),
        ).fetchone()
        return {
            "event": item,
            "sourceFacts": source_facts,
            "transitions": transitions,
            "currentState": dict(current) if current else None,
            "sourceWrite": False,
            "formalPublication": False,
        }
    finally:
        connection.close()



def build_router() -> APIRouter:
    router = APIRouter()
    router.add_api_route("/api/semantic-events/summary", semantic_events_summary, methods=["GET"])
    router.add_api_route("/api/semantic-events", semantic_events, methods=["GET"])
    router.add_api_route("/api/semantic-events/{event_id}", semantic_event_detail, methods=["GET"])
    return router
