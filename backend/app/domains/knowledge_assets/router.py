"""Knowledge asset query routes (native APIRouter)."""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query

from app.core.auth import require_decision_auth
from app.core.db import unified_semantics_connection
from app.schemas.semantic import (
    KnowledgeCaseRequest,
    KnowledgeExtractionRequest,
    KnowledgeImportRequest,
    KnowledgeReleaseRequest,
    KnowledgeReplayRequest,
    KnowledgeReviewRequest,
)

from .service import (
    build_case_knowledge,
    detect_knowledge_conflicts_payload,
    extract_knowledge_candidate,
    import_knowledge_document_payload,
    knowledge_asset_evidence_payload,
    knowledge_asset_row,
    knowledge_asset_versions_payload,
    knowledge_for_object_payload,
    release_knowledge_assets,
    replay_knowledge_payload,
    retrieve_knowledge_payload,
    review_knowledge_asset,
)


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
    asset_type: Literal[
        "all", "document", "standard", "rule", "sop", "case", "expert", "fragment",
        "terminology", "evaluation_case", "definition",
    ] = "all",
    knowledge_kind: str | None = Query(default=None, alias="knowledgeKind", max_length=80),
    status: str | None = None,
) -> dict[str, Any]:
    connection = unified_semantics_connection()
    try:
        where: list[str] = []
        parameters: list[Any] = []
        if asset_type != "all":
            where.append("asset_type=?")
            parameters.append(asset_type)
        if knowledge_kind and knowledge_kind.strip():
            where.append("knowledge_kind=?")
            parameters.append(knowledge_kind.strip())
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


def knowledge_asset_evidence(asset_id: str) -> dict[str, Any]:
    connection = unified_semantics_connection()
    try:
        try:
            return knowledge_asset_evidence_payload(connection, asset_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
    finally:
        connection.close()


def knowledge_asset_versions(asset_id: str) -> dict[str, Any]:
    connection = unified_semantics_connection()
    try:
        try:
            return knowledge_asset_versions_payload(connection, asset_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
    finally:
        connection.close()


def semantic_object_knowledge(object_type: str, object_key: str) -> dict[str, Any]:
    connection = unified_semantics_connection()
    try:
        return knowledge_for_object_payload(connection, object_type, object_key)
    finally:
        connection.close()


def semantic_knowledge_import(request: KnowledgeImportRequest, _: str = Depends(require_decision_auth)) -> dict[str, Any]:
    try:
        return import_knowledge_document_payload(request)
    except (FileNotFoundError, PermissionError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def semantic_knowledge_extract(asset_id: str, request: KnowledgeExtractionRequest, _: str = Depends(require_decision_auth)) -> dict[str, Any]:
    if asset_id != request.fragment_id:
        raise HTTPException(status_code=400, detail="路径 asset_id 必须与 fragmentId 一致")
    try:
        return extract_knowledge_candidate(request)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def semantic_knowledge_case(request: KnowledgeCaseRequest, _: str = Depends(require_decision_auth)) -> dict[str, Any]:
    try:
        return build_case_knowledge(request)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def semantic_knowledge_review(asset_id: str, request: KnowledgeReviewRequest, actor: str = Depends(require_decision_auth)) -> dict[str, Any]:
    try:
        return review_knowledge_asset(asset_id, request, actor)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def semantic_knowledge_release(request: KnowledgeReleaseRequest, _: str = Depends(require_decision_auth)) -> dict[str, Any]:
    return release_knowledge_assets(request)


def semantic_knowledge_conflicts(_: str = Depends(require_decision_auth)) -> dict[str, Any]:
    return detect_knowledge_conflicts_payload()


def semantic_knowledge_replay(asset_id: str, request: KnowledgeReplayRequest, _: str = Depends(require_decision_auth)) -> dict[str, Any]:
    try:
        return replay_knowledge_payload(asset_id, request.cases)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def semantic_knowledge_retrieve(
    query: str = Query(default="", max_length=1000),
    object_type: str | None = Query(default=None, alias="objectType", max_length=100),
    object_key: str | None = Query(default=None, alias="objectKey", max_length=300),
    limit: int = Query(default=20, ge=1, le=100),
) -> dict[str, Any]:
    connection = unified_semantics_connection()
    try:
        return retrieve_knowledge_payload(connection, query=query, object_type=object_type, object_key=object_key, limit=limit)
    finally:
        connection.close()



def build_router() -> APIRouter:
    router = APIRouter()
    router.add_api_route("/api/knowledge-assets/summary", knowledge_asset_summary, methods=["GET"])
    router.add_api_route("/api/knowledge-assets", knowledge_assets, methods=["GET"])
    router.add_api_route("/api/knowledge-assets/{asset_id}", knowledge_asset_detail, methods=["GET"])
    router.add_api_route("/api/knowledge-assets/{asset_id}/evidence", knowledge_asset_evidence, methods=["GET"])
    router.add_api_route("/api/knowledge-assets/{asset_id}/versions", knowledge_asset_versions, methods=["GET"])
    # V3 aliases expose the same read-only registry without creating a second
    # knowledge store or a second lifecycle implementation.
    router.add_api_route("/api/semantic/knowledge", knowledge_assets, methods=["GET"])
    router.add_api_route("/api/semantic/knowledge/{asset_id}", knowledge_asset_detail, methods=["GET"])
    router.add_api_route("/api/semantic/knowledge/{asset_id}/evidence", knowledge_asset_evidence, methods=["GET"])
    router.add_api_route("/api/semantic/knowledge/{asset_id}/versions", knowledge_asset_versions, methods=["GET"])
    router.add_api_route("/api/semantic/object/{object_type}/{object_key}/knowledge", semantic_object_knowledge, methods=["GET"])
    router.add_api_route("/api/semantic/knowledge/import", semantic_knowledge_import, methods=["POST"])
    router.add_api_route("/api/semantic/knowledge/cases", semantic_knowledge_case, methods=["POST"])
    router.add_api_route("/api/semantic/knowledge/releases", semantic_knowledge_release, methods=["POST"])
    router.add_api_route("/api/semantic/knowledge/conflicts/detect", semantic_knowledge_conflicts, methods=["POST"])
    router.add_api_route("/api/semantic/knowledge/retrieve", semantic_knowledge_retrieve, methods=["GET"])
    router.add_api_route("/api/semantic/knowledge/{asset_id}/extract", semantic_knowledge_extract, methods=["POST"])
    router.add_api_route("/api/semantic/knowledge/{asset_id}/review", semantic_knowledge_review, methods=["POST"])
    router.add_api_route("/api/semantic/knowledge/{asset_id}/replay", semantic_knowledge_replay, methods=["POST"])
    return router
