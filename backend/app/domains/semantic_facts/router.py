"""Semantic fact query routes (native APIRouter)."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.core.db import unified_semantics_connection


def semantic_facts_summary() -> dict[str, Any]:
    """Return the explainable-fact pipeline state from the local read-only layer."""
    connection = unified_semantics_connection()
    try:
        latest_run = connection.execute(
            "SELECT * FROM semantic_fact_layer_run ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        if latest_run is None:
            raise HTTPException(status_code=503, detail="事实语义层没有构建批次")
        fact_statuses = [dict(row) for row in connection.execute(
            "SELECT status AS value,count(*) AS count FROM semantic_fact GROUP BY status ORDER BY status"
        ).fetchall()]
        fact_types = [dict(row) for row in connection.execute(
            "SELECT fact_type AS value,count(*) AS count FROM semantic_fact GROUP BY fact_type ORDER BY fact_type"
        ).fetchall()]
        decision_statuses = [dict(row) for row in connection.execute(
            "SELECT status AS value,count(*) AS count FROM semantic_rule_decision GROUP BY status ORDER BY status"
        ).fetchall()]
        action_statuses = [dict(row) for row in connection.execute(
            "SELECT status AS value,count(*) AS count FROM semantic_escalation_action GROUP BY status ORDER BY status"
        ).fetchall()]
        logic_rule_count = int(connection.execute(
            "SELECT count(*) FROM knowledge_asset WHERE asset_type='rule'"
        ).fetchone()[0])
        logic_ready_count = int(connection.execute(
            """
            SELECT count(*) FROM machine_semantic_contract c
            JOIN knowledge_asset_version v ON v.asset_version_id=c.asset_version_id
            JOIN knowledge_asset a ON a.asset_id=v.asset_id
            WHERE a.asset_type='rule' AND c.status='ready'
            """
        ).fetchone()[0])
        logic_action_spec_count = int(connection.execute(
            """
            SELECT count(*) FROM machine_action_spec s
            JOIN knowledge_asset_version v ON v.asset_version_id=s.asset_version_id
            JOIN knowledge_asset a ON a.asset_id=v.asset_id
            WHERE a.asset_type='rule'
            """
        ).fetchone()[0])
        deterministic_rule_count = int(connection.execute(
            "SELECT count(*) FROM semantic_logic_rule WHERE status='enabled'"
        ).fetchone()[0])
        reasoning_run = connection.execute(
            "SELECT * FROM semantic_reasoning_run ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        return {
            "latestRun": dict(latest_run),
            "factCount": int(connection.execute("SELECT count(*) FROM semantic_fact").fetchone()[0]),
            "observedFactCount": int(connection.execute("SELECT count(*) FROM semantic_fact WHERE status='observed'").fetchone()[0]),
            "derivedFactCount": int(connection.execute("SELECT count(*) FROM semantic_fact WHERE status IN ('derived','accepted')").fetchone()[0]),
            "derivationCount": int(connection.execute("SELECT count(*) FROM semantic_fact_derivation").fetchone()[0]),
            "decisionCount": int(connection.execute("SELECT count(*) FROM semantic_rule_decision").fetchone()[0]),
            "actionCount": int(connection.execute("SELECT count(*) FROM semantic_escalation_action").fetchone()[0]),
            "logicRuleCount": logic_rule_count,
            "logicReadyCount": logic_ready_count,
            "logicActionSpecCount": logic_action_spec_count,
            "deterministicRuleCount": deterministic_rule_count,
            "latestReasoningRun": dict(reasoning_run) if reasoning_run else None,
            "factStatuses": fact_statuses,
            "factTypes": fact_types,
            "decisionStatuses": decision_statuses,
            "actionStatuses": action_statuses,
            "sourceWrite": False,
            "formalPublication": False,
            "policy": [
                "观察事实必须保留源系统、源表、源行和快照证据",
                "规则判断和派生事实必须记录输入事实、规则版本、解释和约束结果",
                "升级行动只生成本地计划，不直接写源系统或创建正式工单",
                "当前 91℃、DEF001、WO001 等示例不会在没有来源字段时被推断为真实事实",
            ],
        }
    finally:
        connection.close()


def semantic_facts(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    fact_type: str = "all",
    status: str = "all",
    search: str | None = None,
) -> dict[str, Any]:
    connection = unified_semantics_connection()
    try:
        where: list[str] = []
        parameters: list[Any] = []
        if fact_type.strip() and fact_type != "all":
            where.append("fact_type=?")
            parameters.append(fact_type.strip())
        if status.strip() and status != "all":
            where.append("status=?")
            parameters.append(status.strip())
        if search and search.strip():
            value = f"%{search.strip()}%"
            where.append("(fact_id LIKE ? OR subject_key LIKE ? OR predicate LIKE ? OR source_schema LIKE ? OR source_table LIKE ? OR source_row_id LIKE ?)")
            parameters.extend([value] * 6)
        where_sql = f"WHERE {' AND '.join(where)}" if where else ""
        total = int(connection.execute(f"SELECT count(*) FROM semantic_fact {where_sql}", parameters).fetchone()[0])
        rows = connection.execute(
            f"""
            SELECT * FROM semantic_fact
            {where_sql}
            ORDER BY created_at DESC,fact_id
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


def semantic_fact_detail(fact_id: str) -> dict[str, Any]:
    connection = unified_semantics_connection()
    try:
        fact = connection.execute("SELECT * FROM semantic_fact WHERE fact_id=?", (fact_id,)).fetchone()
        if fact is None:
            raise HTTPException(status_code=404, detail="语义事实不存在")
        derivations = [dict(row) for row in connection.execute(
            "SELECT * FROM semantic_fact_derivation WHERE output_fact_id=? ORDER BY created_at DESC",
            (fact_id,),
        ).fetchall()]
        decisions = [dict(row) for row in connection.execute(
            "SELECT * FROM semantic_rule_decision WHERE input_fact_ids_json LIKE ? OR decision_id IN (SELECT decision_id FROM semantic_fact_derivation WHERE output_fact_id=?) ORDER BY created_at DESC",
            (f"%{fact_id}%", fact_id),
        ).fetchall()]
        decision_ids = [row["decision_id"] for row in decisions]
        actions: list[dict[str, Any]] = []
        if decision_ids:
            marks = ",".join("?" for _ in decision_ids)
            actions = [dict(row) for row in connection.execute(
                f"SELECT * FROM semantic_escalation_action WHERE decision_id IN ({marks}) ORDER BY created_at DESC",
                decision_ids,
            ).fetchall()]
        return {
            "fact": dict(fact),
            "derivations": derivations,
            "decisions": decisions,
            "actions": actions,
            "sourceWrite": False,
            "formalPublication": False,
        }
    finally:
        connection.close()



def build_router() -> APIRouter:
    router = APIRouter()
    router.add_api_route("/api/semantic-facts/summary", semantic_facts_summary, methods=["GET"])
    router.add_api_route("/api/semantic-facts", semantic_facts, methods=["GET"])
    router.add_api_route("/api/semantic-facts/{fact_id}", semantic_fact_detail, methods=["GET"])
    return router
