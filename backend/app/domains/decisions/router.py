"""Semantic decision and action plan routes (native APIRouter)."""
from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from app.core.auth import require_decision_auth
from app.core.db import unified_semantics_connection, unified_semantics_write_connection
from app.core.utils import utc_now
from app.schemas.semantic import SemanticActionApprovalRequest


def decision_layer_tables(connection: sqlite3.Connection) -> set[str]:
    return {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}


def semantic_decisions_summary() -> dict[str, Any]:
    """Summarize the local Decision -> ActionPlan -> Approval boundary."""
    connection = unified_semantics_connection()
    try:
        tables = decision_layer_tables(connection)
        required = {"semantic_action_plan", "semantic_action_approval", "semantic_decision_layer_run"}
        if not required.issubset(tables):
            raise HTTPException(status_code=503, detail="决策行动层尚未初始化，请先运行决策层构建脚本")
        latest = connection.execute("SELECT * FROM semantic_decision_layer_run ORDER BY created_at DESC LIMIT 1").fetchone()
        if latest is None:
            raise HTTPException(status_code=503, detail="决策行动层没有构建批次")
        decision_statuses = [dict(row) for row in connection.execute(
            "SELECT status AS value,count(*) AS count FROM semantic_rule_decision GROUP BY status ORDER BY status"
        ).fetchall()]
        plan_statuses = [dict(row) for row in connection.execute(
            "SELECT status AS value,count(*) AS count FROM semantic_action_plan GROUP BY status ORDER BY status"
        ).fetchall()]
        approval_statuses = [dict(row) for row in connection.execute(
            "SELECT status AS value,count(*) AS count FROM semantic_action_approval GROUP BY status ORDER BY status"
        ).fetchall()]
        return {
            "latestRun": dict(latest),
            "decisionCount": int(connection.execute("SELECT count(*) FROM semantic_rule_decision WHERE status IN ('accepted','proposed','needs_review')").fetchone()[0]),
            "actionRuleCount": int(connection.execute("SELECT count(*) FROM semantic_action_rule WHERE status='enabled'").fetchone()[0]) if "semantic_action_rule" in tables else 0,
            "actionRuleMatchCount": int((latest["action_rule_match_count"] if latest and "action_rule_match_count" in latest.keys() else 0) or 0),
            "riskEvidenceCount": int((latest["risk_evidence_count"] if latest and "risk_evidence_count" in latest.keys() else 0) or 0),
            "actionPlanCount": int(connection.execute("SELECT count(*) FROM semantic_action_plan").fetchone()[0]),
            "pendingApprovalCount": int(connection.execute("SELECT count(*) FROM semantic_action_plan WHERE status='PENDING_APPROVAL'").fetchone()[0]),
            "approvalCount": int(connection.execute("SELECT count(*) FROM semantic_action_approval").fetchone()[0]),
            "decisionStatuses": decision_statuses,
            "planStatuses": plan_statuses,
            "approvalStatuses": approval_statuses,
            "sourceWrite": False,
            "formalPublication": False,
            "policy": [
                "只消费已接受或待复核的规则判断，不从自然语言直接生成行动",
                "没有明确 semantic_escalation_action 的 requires_action 判断不会被猜测成工单",
                "ActionPlan 只能写本地语义覆盖层，审批不会执行源系统写入",
                "审批与执行分离；当前批准只形成本地审批凭据，不调用源系统",
            ],
        }
    finally:
        connection.close()


def semantic_decisions(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    status: str = "all",
    requires_action: str = "all",
    search: str | None = None,
) -> dict[str, Any]:
    connection = unified_semantics_connection()
    try:
        if "semantic_action_plan" not in decision_layer_tables(connection):
            raise HTTPException(status_code=503, detail="决策行动层尚未初始化")
        where: list[str] = []
        parameters: list[Any] = []
        if status and status != "all":
            where.append("d.status=?")
            parameters.append(status)
        if requires_action in {"0", "1"}:
            where.append("d.requires_action=?")
            parameters.append(int(requires_action))
        if search and search.strip():
            term = f"%{search.strip()}%"
            where.append("(d.decision_id LIKE ? OR d.subject_key LIKE ? OR d.decision LIKE ? OR d.rule_asset_id LIKE ? OR d.explanation LIKE ?)")
            parameters.extend([term] * 5)
        where_sql = f"WHERE {' AND '.join(where)}" if where else ""
        total = int(connection.execute(f"SELECT count(*) FROM semantic_rule_decision d {where_sql}", parameters).fetchone()[0])
        rows = connection.execute(
            f"""
            SELECT d.*,p.plan_id,p.action_type,p.status AS plan_status,p.requires_approval AS plan_requires_approval
            FROM semantic_rule_decision d
            LEFT JOIN semantic_action_plan p ON p.decision_id=d.decision_id
            {where_sql}
            ORDER BY d.created_at DESC,d.decision_id
            LIMIT ? OFFSET ?
            """,
            parameters + [page_size, (page - 1) * page_size],
        ).fetchall()
        return {"items": [dict(row) for row in rows], "total": total, "page": page, "pageSize": page_size, "sourceWrite": False, "formalPublication": False}
    finally:
        connection.close()


def semantic_action_plans(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    status: str = "all",
    search: str | None = None,
) -> dict[str, Any]:
    connection = unified_semantics_connection()
    try:
        if "semantic_action_plan" not in decision_layer_tables(connection):
            raise HTTPException(status_code=503, detail="决策行动层尚未初始化")
        where: list[str] = []
        parameters: list[Any] = []
        if status and status != "all":
            where.append("p.status=?")
            parameters.append(status)
        if search and search.strip():
            term = f"%{search.strip()}%"
            where.append("(p.plan_id LIKE ? OR p.decision_id LIKE ? OR p.action_type LIKE ? OR p.target_key LIKE ? OR p.reason LIKE ?)")
            parameters.extend([term] * 5)
        where_sql = f"WHERE {' AND '.join(where)}" if where else ""
        total = int(connection.execute(f"SELECT count(*) FROM semantic_action_plan p {where_sql}", parameters).fetchone()[0])
        rows = connection.execute(
            f"""
            SELECT p.*,a.status AS approval_status,a.reviewer,a.comment,a.approval_receipt
            FROM semantic_action_plan p
            LEFT JOIN semantic_action_approval a ON a.plan_id=p.plan_id
            {where_sql}
            ORDER BY p.updated_at DESC,p.plan_id
            LIMIT ? OFFSET ?
            """,
            parameters + [page_size, (page - 1) * page_size],
        ).fetchall()
        return {"items": [dict(row) for row in rows], "total": total, "page": page, "pageSize": page_size, "sourceWrite": False, "formalPublication": False}
    finally:
        connection.close()


def semantic_action_plans_summary() -> dict[str, Any]:
    """Provide the same governed count for callers focused on action plans."""
    return semantic_decisions_summary()


def semantic_action_catalog() -> dict[str, Any]:
    """Return the first-class business Action catalog.

    Actions describe *what* to do in business terms; adapters describe *how*
    a particular system implements the action.  The catalog itself never
    grants execution rights.
    """
    connection = unified_semantics_connection()
    try:
        if "semantic_action_definition" not in decision_layer_tables(connection):
            raise HTTPException(status_code=503, detail="语义 Action 目录尚未初始化，请先运行 build_semantic_action_catalog.py")
        rows = connection.execute(
            "SELECT * FROM semantic_action_definition ORDER BY action_id"
        ).fetchall()
        items: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            for key in ("allowed_when_json", "required_facts_json", "permission_scope_json",
                        "adapter_mappings_json", "effects_json", "execution_states_json"):
                try:
                    item[key] = json.loads(item.get(key) or "{}")
                except json.JSONDecodeError:
                    item[key] = {}
            items.append(item)
        return {
            "items": items,
            "total": len(items),
            "sourceWrite": False,
            "formalPublication": False,
        }
    finally:
        connection.close()


def semantic_action_plan_detail(plan_id: str) -> dict[str, Any]:
    connection = unified_semantics_connection()
    try:
        plan = connection.execute("SELECT * FROM semantic_action_plan WHERE plan_id=?", (plan_id,)).fetchone()
        if plan is None:
            raise HTTPException(status_code=404, detail="行动计划不存在")
        decision = connection.execute("SELECT * FROM semantic_rule_decision WHERE decision_id=?", (plan["decision_id"],)).fetchone()
        approval = connection.execute("SELECT * FROM semantic_action_approval WHERE plan_id=?", (plan_id,)).fetchone()
        payload = dict(plan)
        try:
            payload["payload"] = json.loads(payload.pop("payload_json") or "{}")
        except json.JSONDecodeError:
            payload["payload"] = {}
        return {"plan": payload, "decision": dict(decision) if decision else None, "approval": dict(approval) if approval else None, "sourceWrite": False, "formalPublication": False}
    finally:
        connection.close()


def review_semantic_action_plan(plan_id: str, request: SemanticActionApprovalRequest, actor: str = Depends(require_decision_auth)) -> dict[str, Any]:
    """Record local approval only; never execute the planned action."""
    connection = unified_semantics_write_connection()
    try:
        plan = connection.execute("SELECT * FROM semantic_action_plan WHERE plan_id=?", (plan_id,)).fetchone()
        if plan is None:
            raise HTTPException(status_code=404, detail="行动计划不存在")
        if plan["status"] != "PENDING_APPROVAL":
            raise HTTPException(status_code=409, detail=f"当前行动计划状态为 {plan['status']}，不能重复审批")
        approval = connection.execute("SELECT * FROM semantic_action_approval WHERE plan_id=?", (plan_id,)).fetchone()
        if approval is None:
            raise HTTPException(status_code=409, detail="行动计划缺少审批任务，不能审批")
        reviewed_at = utc_now()
        receipt = f"semantic-action-approval-{uuid.uuid4().hex}"
        final_status = "APPROVED" if request.decision == "approved" else "REJECTED"
        connection.execute(
            "UPDATE semantic_action_approval SET status=?,reviewer=?,comment=?,approval_receipt=?,reviewed_at=? WHERE plan_id=?",
            (final_status, request.reviewer.strip() or actor, request.comment.strip(), receipt, reviewed_at, plan_id),
        )
        connection.execute("UPDATE semantic_action_plan SET status=?,updated_at=? WHERE plan_id=?", (final_status, reviewed_at, plan_id))
        connection.commit()
        return {"planId": plan_id, "status": final_status, "approvalReceipt": receipt, "sourceWrite": False, "formalPublication": False, "note": "审批仅写本地审批记录，未执行行动、未写源系统"}
    finally:
        connection.close()



def build_router() -> APIRouter:
    router = APIRouter()
    router.add_api_route("/api/semantic-decisions/summary", semantic_decisions_summary, methods=["GET"])
    router.add_api_route("/api/semantic-decisions", semantic_decisions, methods=["GET"])
    router.add_api_route("/api/semantic-action-plans", semantic_action_plans, methods=["GET"])
    router.add_api_route("/api/semantic-action-plans/summary", semantic_action_plans_summary, methods=["GET"])
    router.add_api_route("/api/semantic-actions/catalog", semantic_action_catalog, methods=["GET"])
    router.add_api_route("/api/semantic-action-plans/{plan_id}", semantic_action_plan_detail, methods=["GET"])
    router.add_api_route("/api/semantic-action-plans/{plan_id}/approval", review_semantic_action_plan, methods=["POST"])
    return router
