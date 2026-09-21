"""Semantic status dictionary and state routes (native APIRouter)."""
from __future__ import annotations

import hashlib
import importlib.util
import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from app.core.auth import require_decision_auth
from app.core.config import SYSTEM_ROOT, UNIFIED_SEMANTICS_DB
from app.core.db import unified_semantics_connection, unified_semantics_write_connection
from app.core.utils import utc_now
from app.schemas.semantic import DefectStatusReviewRequest, SemanticStateReplayRequest


def semantic_status_dictionary_summary() -> dict[str, Any]:
    connection = unified_semantics_connection()
    try:
        latest = connection.execute(
            "SELECT * FROM semantic_status_dictionary_run ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        if latest is None:
            raise HTTPException(status_code=503, detail="缺陷状态字典没有构建批次")
        by_system = [dict(row) for row in connection.execute(
            "SELECT source_schema AS value,count(*) AS count FROM semantic_status_dictionary GROUP BY source_schema ORDER BY source_schema"
        ).fetchall()]
        by_status = [dict(row) for row in connection.execute(
            "SELECT mapping_status AS value,count(*) AS count FROM semantic_status_dictionary GROUP BY mapping_status ORDER BY mapping_status"
        ).fetchall()]
        canonical_states = [dict(row) for row in connection.execute(
            """
            SELECT canonical_state AS value,display_name,description,is_terminal,sort_order
            FROM semantic_canonical_state
            WHERE state_domain='DEFECT' AND status='active'
            ORDER BY sort_order
            """
        ).fetchall()]
        replay = connection.execute(
            "SELECT * FROM semantic_status_replay_run ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        return {
            "latestRun": dict(latest),
            "candidateCount": int(connection.execute("SELECT count(*) FROM semantic_status_dictionary").fetchone()[0]),
            "pendingCount": int(connection.execute("SELECT count(*) FROM semantic_status_dictionary WHERE mapping_status='pending'").fetchone()[0]),
            "approvedCount": int(connection.execute("SELECT count(*) FROM semantic_status_dictionary WHERE mapping_status='approved'").fetchone()[0]),
            "rejectedCount": int(connection.execute("SELECT count(*) FROM semantic_status_dictionary WHERE mapping_status='rejected'").fetchone()[0]),
            "evidenceRowCount": int(connection.execute("SELECT coalesce(sum(evidence_count),0) FROM semantic_status_dictionary").fetchone()[0]),
            "systems": by_system,
            "mappingStatuses": by_status,
            "canonicalStates": canonical_states,
            "latestReplay": dict(replay) if replay else None,
            "sourceWrite": False,
            "formalPublication": False,
            "policy": [
                "保留 HD/XNY 原始状态值，不跨系统自动合并或改写",
                "只有明确业务字典或责任人确认后，才映射到标准缺陷状态",
                "未知、空值和编码冲突保持 pending，不自动生成行动",
                "状态含义确认只写本地语义字典，不修改源表",
            ],
        }
    finally:
        connection.close()


def semantic_status_dictionary(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    source_schema: str = "all",
    source_table: str = "all",
    mapping_status: str = "all",
    search: str | None = None,
) -> dict[str, Any]:
    connection = unified_semantics_connection()
    try:
        where: list[str] = []
        parameters: list[Any] = []
        if source_schema.strip() and source_schema != "all":
            where.append("source_schema=?")
            parameters.append(source_schema.strip())
        if source_table.strip() and source_table != "all":
            where.append("source_table=?")
            parameters.append(source_table.strip())
        if mapping_status.strip() and mapping_status != "all":
            where.append("mapping_status=?")
            parameters.append(mapping_status.strip())
        if search and search.strip():
            value = f"%{search.strip()}%"
            where.append("(status_id LIKE ? OR raw_status LIKE ? OR canonical_state LIKE ? OR business_meaning LIKE ? OR mapping_notes LIKE ?)")
            parameters.extend([value] * 5)
        where_sql = f"WHERE {' AND '.join(where)}" if where else ""
        total = int(connection.execute(f"SELECT count(*) FROM semantic_status_dictionary {where_sql}", parameters).fetchone()[0])
        rows = connection.execute(
            f"""
            SELECT * FROM semantic_status_dictionary
            {where_sql}
            ORDER BY mapping_status,source_schema,source_table,evidence_count DESC,status_id
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


def semantic_status_dictionary_detail(status_id: str) -> dict[str, Any]:
    connection = unified_semantics_connection()
    try:
        row = connection.execute(
            "SELECT * FROM semantic_status_dictionary WHERE status_id=?", (status_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="缺陷状态字典条目不存在")
        item = dict(row)
        try:
            examples = json.loads(item.get("example_records_json") or "[]")
        except json.JSONDecodeError:
            examples = []
        item["examples"] = examples
        reviews = [dict(review) for review in connection.execute(
            "SELECT * FROM semantic_status_mapping_review WHERE status_id=? ORDER BY mapping_version DESC",
            (status_id,),
        ).fetchall()]
        return {"item": item, "reviews": reviews, "sourceWrite": False, "formalPublication": False}
    finally:
        connection.close()


def execute_status_mapping_replay_local() -> dict[str, Any]:
    script_path = SYSTEM_ROOT / "replay_defect_status_mapping.py"
    spec = importlib.util.spec_from_file_location("semantic_status_mapping_replay", script_path)
    if spec is None or spec.loader is None:
        raise HTTPException(status_code=503, detail="缺陷状态回放器不可用")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.replay(UNIFIED_SEMANTICS_DB)


def replay_semantic_status_dictionary() -> dict[str, Any]:
    result = execute_status_mapping_replay_local()
    return result


def review_semantic_status_dictionary(status_id: str, request: DefectStatusReviewRequest, actor: str = Depends(require_decision_auth)) -> dict[str, Any]:
    connection = unified_semantics_write_connection()
    try:
        row = connection.execute(
            "SELECT * FROM semantic_status_dictionary WHERE status_id=?", (status_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="缺陷状态字典条目不存在")
        canonical_state = (request.canonical_state or "").strip().upper() or None
        canonical = None
        if canonical_state:
            canonical = connection.execute(
                """
                SELECT * FROM semantic_canonical_state
                WHERE state_domain='DEFECT' AND canonical_state=? AND status='active'
                """,
                (canonical_state,),
            ).fetchone()
        if request.decision == "approved" and canonical is None:
            raise HTTPException(status_code=400, detail="确认状态映射时必须选择有效的标准缺陷状态")
        meaning = (request.business_meaning or "").strip() or (canonical["display_name"] if canonical else None)
        reviewed_at = utc_now()
        mapping_version = int(row["mapping_version"] or 0) + 1
        review_id = f"SSMR-{hashlib.sha256(f'{status_id}|{mapping_version}|{reviewed_at}'.encode('utf-8')).hexdigest()[:24]}"
        connection.execute(
            """
            INSERT INTO semantic_status_mapping_review(
              review_id,status_id,mapping_version,previous_mapping_status,
              previous_canonical_state,decision,canonical_state,business_meaning,
              notes,reviewer,reviewed_at,source_write,formal_publication
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,0,0)
            """,
            (
                review_id,status_id,mapping_version,row["mapping_status"],row["canonical_state"],
                request.decision,canonical_state if request.decision == "approved" else None,
                meaning if request.decision == "approved" else None,request.notes.strip(),
                request.reviewer.strip() or actor,reviewed_at,
            ),
        )
        connection.execute(
            """
            UPDATE semantic_status_dictionary
            SET mapping_status=?,canonical_state=?,business_meaning=?,mapping_notes=?,
              mapping_version=?,reviewer=?,reviewed_at=?,updated_at=?
            WHERE status_id=?
            """,
            (
                request.decision,canonical_state if request.decision == "approved" else None,
                meaning if request.decision == "approved" else None,request.notes.strip(),mapping_version,
                request.reviewer.strip() or actor,reviewed_at,reviewed_at,status_id,
            ),
        )
        connection.commit()
        updated = connection.execute("SELECT * FROM semantic_status_dictionary WHERE status_id=?", (status_id,)).fetchone()
        return {"item": dict(updated), "reviewId": review_id, "sourceWrite": False, "formalPublication": False}
    finally:
        connection.close()


def semantic_states_summary() -> dict[str, Any]:
    connection = unified_semantics_connection()
    try:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if "semantic_current_state" not in tables or "semantic_state_transition_run" not in tables:
            raise HTTPException(status_code=503, detail="状态迁移层尚未初始化")
        latest = connection.execute(
            "SELECT * FROM semantic_state_transition_run ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        by_state = [dict(row) for row in connection.execute(
            """
            SELECT current_state AS value,display_name,count(*) AS count
            FROM semantic_current_state
            WHERE status='current'
            GROUP BY current_state,display_name
            ORDER BY current_state
            """
        ).fetchall()]
        return {
            "latestRun": dict(latest) if latest else None,
            "currentStateCount": int(connection.execute("SELECT count(*) FROM semantic_current_state WHERE status='current'").fetchone()[0]),
            "transitionCount": int(connection.execute("SELECT count(*) FROM semantic_state_transition").fetchone()[0]),
            "reviewTransitionCount": int(connection.execute("SELECT count(*) FROM semantic_state_transition WHERE status='needs_review'").fetchone()[0]),
            "differenceCount": int(connection.execute("SELECT count(*) FROM semantic_state_replay_diff WHERE status IN ('reported','needs_review')").fetchone()[0]) if "semantic_state_replay_diff" in tables else 0,
            "states": by_state,
            "sourceWrite": False,
            "formalPublication": False,
            "policy": [
                "只消费已确认的 canonical_defect_state 事实",
                "无时间顺序证据的冲突状态不覆盖当前状态",
                "状态迁移和当前状态均属于本地语义覆盖层",
            ],
        }
    finally:
        connection.close()


def replay_semantic_states(request: SemanticStateReplayRequest) -> dict[str, Any]:
    """Replay state transitions locally, optionally for one subject only."""
    executor_path = SYSTEM_ROOT / "execute_state_transitions.py"
    spec = importlib.util.spec_from_file_location("semantic_state_replay_runtime", executor_path)
    if spec is None or spec.loader is None:
        raise HTTPException(status_code=503, detail="状态回放执行器不可用")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.execute(UNIFIED_SEMANTICS_DB, request.subject_type, request.subject_key)


def semantic_state_replay_diffs(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    run_id: str | None = None,
    difference_type: str | None = None,
) -> dict[str, Any]:
    connection = unified_semantics_connection()
    try:
        clauses: list[str] = []
        parameters: list[Any] = []
        if run_id:
            clauses.append("replay_run_id=?")
            parameters.append(run_id)
        if difference_type:
            clauses.append("difference_type=?")
            parameters.append(difference_type)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        total = int(connection.execute(f"SELECT count(*) FROM semantic_state_replay_diff {where}", parameters).fetchone()[0])
        rows = connection.execute(
            f"SELECT * FROM semantic_state_replay_diff {where} ORDER BY created_at DESC,diff_id LIMIT ? OFFSET ?",
            parameters + [page_size, (page - 1) * page_size],
        ).fetchall()
        return {"items": [dict(row) for row in rows], "total": total, "page": page, "pageSize": page_size, "sourceWrite": False, "formalPublication": False}
    finally:
        connection.close()



def build_router() -> APIRouter:
    router = APIRouter()
    router.add_api_route("/api/semantic-status-dictionary/summary", semantic_status_dictionary_summary, methods=["GET"])
    router.add_api_route("/api/semantic-status-dictionary", semantic_status_dictionary, methods=["GET"])
    router.add_api_route("/api/semantic-status-dictionary/{status_id}", semantic_status_dictionary_detail, methods=["GET"])
    router.add_api_route("/api/semantic-status-dictionary/replay", replay_semantic_status_dictionary, methods=["POST"])
    router.add_api_route("/api/semantic-status-dictionary/{status_id}/review", review_semantic_status_dictionary, methods=["POST"])
    router.add_api_route("/api/semantic-states/summary", semantic_states_summary, methods=["GET"])
    router.add_api_route("/api/semantic-states/replay", replay_semantic_states, methods=["POST"])
    router.add_api_route("/api/semantic-states/replay-diffs", semantic_state_replay_diffs, methods=["GET"])
    return router
