"""Semantic execution routes (native APIRouter)."""
from __future__ import annotations

import hashlib
import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from app.core.auth import require_decision_auth
from app.core.db import unified_semantics_connection, unified_semantics_write_connection
from app.core.utils import utc_now
from app.schemas.semantic import SemanticExecutionDispatchRequest, SemanticExecutionPreviewRequest


def semantic_execution_summary() -> dict[str, Any]:
    connection = unified_semantics_connection()
    try:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if "semantic_action_run" not in tables:
            return {"runCount": 0, "statuses": [], "latest": None, "adapterCount": 0, "ledgerCount": 0, "sourceWrite": False, "formalPublication": False}
        statuses = [dict(row) for row in connection.execute(
            "SELECT status AS value,count(*) AS count FROM semantic_action_run GROUP BY status ORDER BY status"
        ).fetchall()]
        latest = connection.execute(
            "SELECT run_id,asset_id,asset_version_id,action_id,mode,status,target_count,created_at FROM semantic_action_run ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        return {
            "runCount": int(connection.execute("SELECT count(*) FROM semantic_action_run").fetchone()[0]),
            "statuses": statuses,
            "latest": dict(latest) if latest else None,
            "adapterCount": int(connection.execute("SELECT count(*) FROM semantic_execution_adapter").fetchone()[0]) if "semantic_execution_adapter" in tables else 0,
            "ledgerCount": int(connection.execute("SELECT count(*) FROM semantic_execution_ledger").fetchone()[0]) if "semantic_execution_ledger" in tables else 0,
            "sourceWrite": False,
            "formalPublication": False,
        }
    finally:
        connection.close()


def semantic_execution_dispatch(request: SemanticExecutionDispatchRequest, actor: str = Depends(require_decision_auth)) -> dict[str, Any]:
    """Create a governed execution-ledger entry; never calls a source system."""
    connection = unified_semantics_write_connection()
    try:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        required = {"semantic_execution_adapter", "semantic_execution_ledger"}
        if not required.issubset(tables):
            raise HTTPException(status_code=503, detail="执行账本尚未初始化，请先运行执行层初始化")
        existing = connection.execute("SELECT * FROM semantic_execution_ledger WHERE idempotency_key=?", (request.idempotency_key,)).fetchone()
        if existing:
            return {"execution": dict(existing), "idempotentReplay": True, "sourceWrite": False, "formalPublication": False}
        adapter = connection.execute("SELECT * FROM semantic_execution_adapter WHERE adapter_id=? AND status='active'", (request.adapter_id,)).fetchone()
        if adapter is None:
            raise HTTPException(status_code=404, detail="执行适配器不存在或未启用")
        plan = connection.execute("SELECT * FROM semantic_action_plan WHERE plan_id=?", (request.action_plan_id,)).fetchone() if request.action_plan_id else None
        if plan is not None and plan["requires_approval"] and plan["status"] != "APPROVED":
            raise HTTPException(status_code=409, detail="行动计划尚未完成独立审批，不能进入执行台账")
        execution_id = f"EXEC-{hashlib.sha256(request.idempotency_key.encode('utf-8')).hexdigest()[:24]}"
        created = utc_now()
        response = {"mode": adapter["mode"], "adapter": adapter["adapter_id"], "actionRunId": request.action_run_id, "actionPlanId": request.action_plan_id, "source_write": False, "formal_publication": False, "note": "当前适配器仅生成本地执行台账，不调用外部系统", "operator": actor}
        connection.execute(
            """INSERT INTO semantic_execution_ledger(
              execution_id,action_run_id,action_plan_id,approval_receipt,adapter_id,
              idempotency_key,status,external_execution_ref,request_json,response_json,
              error_code,error_message,source_write,formal_publication,created_at,updated_at,completed_at
            ) VALUES (?,?,?,?,?,?, 'planned',NULL,?,?,?,?,0,0,?,?,NULL)""",
            (execution_id, request.action_run_id, request.action_plan_id, request.approval_receipt, request.adapter_id, request.idempotency_key, json.dumps(request.request_payload, ensure_ascii=False), json.dumps(response, ensure_ascii=False), None, None, created, created),
        )
        connection.commit()
        execution = connection.execute("SELECT * FROM semantic_execution_ledger WHERE execution_id=?", (execution_id,)).fetchone()
        return {"execution": dict(execution), "idempotentReplay": False, "sourceWrite": False, "formalPublication": False}
    finally:
        connection.close()


def semantic_execution_preview(request: SemanticExecutionPreviewRequest) -> dict[str, Any]:
    connection = unified_semantics_write_connection()
    try:
        existing = connection.execute(
            "SELECT * FROM semantic_action_run WHERE idempotency_key=?",
            (request.idempotency_key,),
        ).fetchone()
        if existing:
            return {
                "runId": existing["run_id"],
                "assetId": existing["asset_id"],
                "assetVersionId": existing["asset_version_id"],
                "mode": existing["mode"],
                "status": existing["status"],
                "targetCount": existing["target_count"],
                "gates": json.loads(existing["gate_summary_json"]),
                "actionPlan": json.loads(existing["action_plan_json"]),
                "idempotentReplay": True,
                "sourceWrite": False,
                "formalPublication": False,
            }
        asset = connection.execute("SELECT * FROM knowledge_asset WHERE asset_id=?", (request.asset_id,)).fetchone()
        if asset is None:
            raise HTTPException(status_code=404, detail="知识资产不存在")
        version = None
        if request.asset_version_id:
            version = connection.execute(
                "SELECT * FROM knowledge_asset_version WHERE asset_version_id=? AND asset_id=?",
                (request.asset_version_id, request.asset_id),
            ).fetchone()
        if version is None:
            version = connection.execute(
                "SELECT * FROM knowledge_asset_version WHERE asset_id=? AND version=?",
                (request.asset_id, asset["current_version"]),
            ).fetchone()
        if version is None:
            version = connection.execute(
                "SELECT * FROM knowledge_asset_version WHERE asset_id=? ORDER BY created_at DESC LIMIT 1",
                (request.asset_id,),
            ).fetchone()
        if version is None:
            raise HTTPException(status_code=409, detail="知识资产没有可执行版本")
        contract = connection.execute(
            "SELECT * FROM machine_semantic_contract WHERE asset_version_id=?",
            (version["asset_version_id"],),
        ).fetchone()
        action = connection.execute(
            "SELECT * FROM machine_action_spec WHERE asset_version_id=? ORDER BY created_at DESC LIMIT 1",
            (version["asset_version_id"],),
        ).fetchone()
        if contract is None or action is None:
            raise HTTPException(status_code=409, detail="机器语义契约不完整，不能生成执行预演")

        gates: list[dict[str, Any]] = []
        gates.append({"key": "contract_exists", "status": "pass", "severity": "high", "message": "判断、计算、约束和行动契约存在"})
        gates.append({"key": "source_write_forbidden", "status": "pass", "severity": "critical", "message": "本次预演 source_write=0，formal_publication=0"})
        if action["action_type"] == "replay_evaluate":
            gates.append({"key": "replay_action", "status": "pass", "severity": "high", "message": "当前行动是回放评估，不执行正式发布"})
        elif int(version["replay_count"] or 0) > 0 and int(version["replay_fail_count"] or 0) == 0:
            gates.append({"key": "replay_before_enable", "status": "pass", "severity": "high", "message": f"回放 {version['replay_count']} 条，失败 0 条"})
        else:
            gates.append({"key": "replay_before_enable", "status": "needs_review", "severity": "high", "message": "当前版本尚无通过的完整回放证据"})
        if int(action["requires_approval"] or 0):
            approval_status = "pass" if asset["status"] in {"approved", "enabled"} else "needs_review"
            gates.append({"key": "approval_before_action", "status": approval_status, "severity": "high", "message": "行动需要独立人工审批" if approval_status != "pass" else "资产已具备审批/启用状态"})
        else:
            gates.append({"key": "approval_before_action", "status": "pass", "severity": "low", "message": "当前回放行动不产生正式发布"})

        hard_blocks = [gate for gate in gates if gate["status"] == "blocked"]
        reviews = [gate for gate in gates if gate["status"] == "needs_review"]
        status = "blocked" if hard_blocks else ("needs_review" if reviews else "ready")
        target_count = min(int(version["preview_count"] or version["replay_count"] or 0), request.sample_size)
        run_id = f"SAR-{hashlib.sha256(request.idempotency_key.encode('utf-8')).hexdigest()[:24]}"
        action_plan = {
            "assetKey": asset["asset_key"],
            "assetType": asset["asset_type"],
            "assetVersion": version["version"],
            "actionType": action["action_type"],
            "targetType": action["target_type"],
            "targetScope": request.target_scope,
            "sampleSize": request.sample_size,
            "requiresApproval": bool(action["requires_approval"]),
            "idempotencyKeyTemplate": action["idempotency_key_template"],
            "note": request.note,
        }
        connection.execute(
            """
            INSERT INTO semantic_action_run(run_id,idempotency_key,asset_id,asset_version_id,action_id,mode,status,
              target_scope,target_count,gate_summary_json,action_plan_json,source_write,formal_publication,created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,0,0,?)
            """,
            (run_id, request.idempotency_key, request.asset_id, version["asset_version_id"], action["action_id"], "preview",
             status, request.target_scope, target_count, json.dumps(gates, ensure_ascii=False),
             json.dumps(action_plan, ensure_ascii=False), utc_now()),
        )
        connection.commit()
        return {
            "runId": run_id,
            "assetId": request.asset_id,
            "assetVersionId": version["asset_version_id"],
            "mode": "preview",
            "status": status,
            "targetCount": target_count,
            "gates": gates,
            "actionPlan": action_plan,
            "idempotentReplay": False,
            "sourceWrite": False,
            "formalPublication": False,
        }
    finally:
        connection.close()



def build_router() -> APIRouter:
    router = APIRouter()
    router.add_api_route("/api/semantic-execution/summary", semantic_execution_summary, methods=["GET"])
    router.add_api_route("/api/semantic-execution/dispatch", semantic_execution_dispatch, methods=["POST"])
    router.add_api_route("/api/semantic-execution/preview", semantic_execution_preview, methods=["POST"])
    return router
