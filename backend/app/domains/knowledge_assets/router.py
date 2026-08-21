"""Knowledge asset query routes (native APIRouter)."""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query

from app.core.db import unified_semantics_connection

from .service import knowledge_asset_row


def knowledge_asset_summary() -> dict[str, Any]:
    connection = unified_semantics_connection()
    try:
        run = connection.execute(
            "SELECT * FROM knowledge_layer_run ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        if run is None:
            raise HTTPException(status_code=503, detail="知识资产关系层没有构建批次")
        by_type = [dict(row) for row in connection.execute(
            "SELECT asset_type AS value,count(*) AS count FROM knowledge_asset GROUP BY asset_type ORDER BY asset_type"
        ).fetchall()]
        by_status = [dict(row) for row in connection.execute(
            "SELECT status AS value,count(*) AS count FROM knowledge_asset GROUP BY status ORDER BY status"
        ).fetchall()]
        issues = [dict(row) for row in connection.execute(
            "SELECT issue_type AS value,count(*) AS count FROM knowledge_asset_issue WHERE status='open' GROUP BY issue_type ORDER BY issue_type"
        ).fetchall()]
        machine_run = connection.execute(
            "SELECT * FROM machine_semantics_run ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        machine_constraints = [dict(row) for row in connection.execute(
            "SELECT on_fail AS value,count(*) AS count FROM machine_constraint_spec GROUP BY on_fail ORDER BY on_fail"
        ).fetchall()]
        object_types = [dict(row) for row in connection.execute(
            "SELECT object_type,display_name,description,parent_object_type,version,status FROM business_object_type ORDER BY object_type"
        ).fetchall()]
        relation_statuses = [dict(row) for row in connection.execute(
            "SELECT status AS value,count(*) AS count FROM business_object_relation GROUP BY status ORDER BY status"
        ).fetchall()]
        return {
            "runId": run["run_id"],
            "assetCount": int(run["asset_count"]),
            "versionCount": int(run["version_count"]),
            "sourceCount": int(run["source_count"]),
            "bindingCount": int(run["binding_count"]),
            "issueCount": int(run["issue_count"]),
            "assetTypes": by_type,
            "statuses": by_status,
            "openIssues": issues,
            "machineContract": dict(machine_run) if machine_run else None,
            "machineConstraintGates": machine_constraints,
            "businessObjectTypes": object_types,
            "businessObjectRelationCount": int(connection.execute("SELECT count(*) FROM business_object_relation").fetchone()[0]),
            "businessObjectRelationStatuses": relation_statuses,
            "sourceWrite": False,
            "formalPublication": False,
            "policy": [
                "知识资产有稳定 asset_key，不以文档文件名作为身份",
                "不同来源保留为 source 记录，差异和冲突不静默覆盖",
                "版本、回放、审批和启用状态分开记录",
                "当前层只写本地关系库，不修改源表或正式结果层",
            ],
        }
    finally:
        connection.close()


def knowledge_assets(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    search: str | None = None,
    asset_type: Literal["all", "rule", "terminology", "evaluation_case", "definition"] = "all",
    status: str | None = None,
) -> dict[str, Any]:
    connection = unified_semantics_connection()
    try:
        where: list[str] = []
        parameters: list[Any] = []
        if asset_type != "all":
            where.append("asset_type=?")
            parameters.append(asset_type)
        if status and status.strip():
            where.append("status=?")
            parameters.append(status.strip())
        if search and search.strip():
            value = f"%{search.strip()}%"
            where.append("(asset_key LIKE ? OR title LIKE ? OR canonical_definition LIKE ? OR current_version LIKE ?)")
            parameters.extend([value] * 4)
        where_sql = f"WHERE {' AND '.join(where)}" if where else ""
        total = int(connection.execute(f"SELECT count(*) FROM knowledge_asset {where_sql}", parameters).fetchone()[0])
        rows = connection.execute(
            f"""
            SELECT * FROM knowledge_asset
            {where_sql}
            ORDER BY updated_at DESC,asset_key
            LIMIT ? OFFSET ?
            """,
            parameters + [page_size, (page - 1) * page_size],
        ).fetchall()
        if not rows:
            return {"items": [], "total": total, "page": page, "pageSize": page_size, "sourceWrite": False, "formalPublication": False}
        ids = [row["asset_id"] for row in rows]
        marks = ",".join("?" for _ in ids)
        source_counts = {row["asset_id"]: int(row["count"]) for row in connection.execute(
            f"SELECT asset_id,count(*) AS count FROM knowledge_asset_source WHERE asset_id IN ({marks}) GROUP BY asset_id", ids
        ).fetchall()}
        issue_counts = {row["asset_id"]: int(row["count"]) for row in connection.execute(
            f"SELECT asset_id,count(*) AS count FROM knowledge_asset_issue WHERE status='open' AND asset_id IN ({marks}) GROUP BY asset_id", ids
        ).fetchall()}
        return {
            "items": [knowledge_asset_row(row, source_counts.get(row["asset_id"], 0), issue_counts.get(row["asset_id"], 0)) for row in rows],
            "total": total,
            "page": page,
            "pageSize": page_size,
            "sourceWrite": False,
            "formalPublication": False,
        }
    finally:
        connection.close()


def knowledge_asset_detail(asset_id: str) -> dict[str, Any]:
    connection = unified_semantics_connection()
    try:
        asset = connection.execute("SELECT * FROM knowledge_asset WHERE asset_id=?", (asset_id,)).fetchone()
        if asset is None:
            raise HTTPException(status_code=404, detail="知识资产不存在")
        versions = [dict(row) for row in connection.execute(
            "SELECT * FROM knowledge_asset_version WHERE asset_id=? ORDER BY created_at DESC,version DESC",
            (asset_id,),
        ).fetchall()]
        parts = [dict(row) for row in connection.execute(
            """
            SELECT p.* FROM knowledge_asset_part p
            JOIN knowledge_asset_version v ON v.asset_version_id=p.asset_version_id
            WHERE v.asset_id=? ORDER BY v.created_at DESC,p.part_type,p.ordinal
            """,
            (asset_id,),
        ).fetchall()]
        sources = [dict(row) for row in connection.execute(
            "SELECT * FROM knowledge_asset_source WHERE asset_id=? ORDER BY created_at,source_kind,source_record_id",
            (asset_id,),
        ).fetchall()]
        bindings = [dict(row) for row in connection.execute(
            "SELECT * FROM knowledge_asset_binding WHERE asset_id=? ORDER BY object_type,object_key",
            (asset_id,),
        ).fetchall()]
        issues = [dict(row) for row in connection.execute(
            "SELECT * FROM knowledge_asset_issue WHERE asset_id=? ORDER BY status,created_at",
            (asset_id,),
        ).fetchall()]
        contract = connection.execute(
            "SELECT * FROM machine_semantic_contract WHERE asset_version_id IN (SELECT asset_version_id FROM knowledge_asset_version WHERE asset_id=?) ORDER BY created_at DESC LIMIT 1",
            (asset_id,),
        ).fetchone()
        decisions = [dict(row) for row in connection.execute(
            "SELECT * FROM machine_decision_spec WHERE asset_version_id IN (SELECT asset_version_id FROM knowledge_asset_version WHERE asset_id=?) ORDER BY created_at DESC",
            (asset_id,),
        ).fetchall()]
        calculations = [dict(row) for row in connection.execute(
            "SELECT * FROM machine_calculation_spec WHERE asset_version_id IN (SELECT asset_version_id FROM knowledge_asset_version WHERE asset_id=?) ORDER BY created_at DESC",
            (asset_id,),
        ).fetchall()]
        constraints = [dict(row) for row in connection.execute(
            "SELECT * FROM machine_constraint_spec WHERE asset_version_id IN (SELECT asset_version_id FROM knowledge_asset_version WHERE asset_id=?) ORDER BY created_at DESC,constraint_key",
            (asset_id,),
        ).fetchall()]
        actions = [dict(row) for row in connection.execute(
            "SELECT * FROM machine_action_spec WHERE asset_version_id IN (SELECT asset_version_id FROM knowledge_asset_version WHERE asset_id=?) ORDER BY created_at DESC",
            (asset_id,),
        ).fetchall()]
        return {
            "asset": knowledge_asset_row(asset, len(sources), sum(1 for item in issues if item["status"] == "open")),
            "versions": versions,
            "parts": parts,
            "sources": sources,
            "bindings": bindings,
            "issues": issues,
            "machineContract": dict(contract) if contract else None,
            "machineDecisions": decisions,
            "machineCalculations": calculations,
            "machineConstraints": constraints,
            "machineActions": actions,
            "sourceWrite": False,
            "formalPublication": False,
        }
    finally:
        connection.close()



def build_router() -> APIRouter:
    router = APIRouter()
    router.add_api_route("/api/knowledge-assets/summary", knowledge_asset_summary, methods=["GET"])
    router.add_api_route("/api/knowledge-assets", knowledge_assets, methods=["GET"])
    router.add_api_route("/api/knowledge-assets/{asset_id}", knowledge_asset_detail, methods=["GET"])
    return router
