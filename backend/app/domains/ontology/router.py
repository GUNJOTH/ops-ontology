"""Ontology meta-model query routes (native APIRouter)."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from app.core.db import unified_semantics_connection

from .service import ontology_meta_tables


def ontology_meta_summary() -> dict[str, Any]:
    """Expose the versioned meta-model and its validation gaps to UI/Agents."""
    connection = unified_semantics_connection()
    try:
        tables = ontology_meta_tables(connection)
        required = {
            "ontology_object_type", "ontology_property_type", "ontology_relation_type",
            "ontology_event_type", "ontology_state_machine", "ontology_transition_rule",
            "ontology_meta_model_run",
        }
        if not required.issubset(tables):
            raise HTTPException(status_code=503, detail="本体元模型尚未初始化，请先运行 build_ontology_meta_model.py")
        latest = connection.execute("SELECT * FROM ontology_meta_model_run ORDER BY created_at DESC LIMIT 1").fetchone()
        core_keys = [
            "device", "site", "center", "specialty", "team", "inspection", "abnormal_inspection",
            "defect", "repeated_defect", "severe_defect", "defect_resolution", "work_order", "work_permit", "human_review",
        ]
        marks = ",".join("?" for _ in core_keys)
        core_rows = connection.execute(
            f"SELECT object_type,display_name,kind,version,review_status,status FROM ontology_object_type WHERE object_type IN ({marks}) ORDER BY object_type",
            core_keys,
        ).fetchall()
        core_by_key = {row["object_type"]: dict(row) for row in core_rows}
        missing_core = [key for key in core_keys if key not in core_by_key]
        relation_statuses = [dict(row) for row in connection.execute(
            "SELECT review_status AS value,count(*) AS count FROM ontology_relation_type WHERE status='active' GROUP BY review_status ORDER BY review_status"
        ).fetchall()]
        event_statuses = [dict(row) for row in connection.execute(
            "SELECT review_status AS value,count(*) AS count FROM ontology_event_type WHERE status='active' GROUP BY review_status ORDER BY review_status"
        ).fetchall()]
        unregistered_relations = [dict(row) for row in connection.execute(
            """SELECT r.predicate, min(r.subject_type) AS subject_type, min(r.object_type) AS object_type
               FROM business_object_relation r
               LEFT JOIN ontology_relation_type t ON t.predicate=r.predicate
               WHERE r.status='accepted' AND (t.predicate IS NULL OR t.review_status='needs_review')
               GROUP BY r.predicate ORDER BY r.predicate"""
        ).fetchall()]
        unregistered_events = [dict(row) for row in connection.execute(
            """SELECT DISTINCT e.event_type, e.subject_type
               FROM semantic_event e
               LEFT JOIN ontology_event_type t ON t.event_type=e.event_type
               WHERE e.status IN ('observed','accepted') AND (t.event_type IS NULL OR t.review_status='needs_review')
               ORDER BY e.event_type"""
        ).fetchall()]
        return {
            "schemaVersion": "ontology-runtime-v1",
            "latestRun": dict(latest) if latest else None,
            "objectTypeCount": int(connection.execute("SELECT count(*) FROM ontology_object_type WHERE status='active'").fetchone()[0]),
            "propertyTypeCount": int(connection.execute("SELECT count(*) FROM ontology_property_type WHERE status='active'").fetchone()[0]),
            "relationTypeCount": int(connection.execute("SELECT count(*) FROM ontology_relation_type WHERE status='active'").fetchone()[0]),
            "eventTypeCount": int(connection.execute("SELECT count(*) FROM ontology_event_type WHERE status='active'").fetchone()[0]),
            "stateMachineCount": int(connection.execute("SELECT count(*) FROM ontology_state_machine WHERE status='active'").fetchone()[0]),
            "transitionRuleCount": int(connection.execute("SELECT count(*) FROM ontology_transition_rule WHERE status='active'").fetchone()[0]),
            "coreObjects": [core_by_key[key] for key in core_keys if key in core_by_key],
            "missingCoreObjects": missing_core,
            "relationReviewStatuses": relation_statuses,
            "eventReviewStatuses": event_statuses,
            "unregisteredRelations": unregistered_relations,
            "unregisteredEvents": unregistered_events,
            "sourceWrite": False,
            "formalPublication": False,
            "policy": [
                "对象、属性、关系、事件和状态机先注册，再允许运行层消费",
                "未知 predicate/event_type 不静默删除，先标记 needs_review",
                "元模型只写本地 SQLite 语义覆盖层，不修改源表",
            ],
        }
    finally:
        connection.close()


def ontology_object_types(kind: str = "all", review_status: str = "all") -> dict[str, Any]:
    connection = unified_semantics_connection()
    try:
        where: list[str] = ["status='active'"]
        parameters: list[Any] = []
        if kind and kind != "all":
            where.append("kind=?")
            parameters.append(kind)
        if review_status and review_status != "all":
            where.append("review_status=?")
            parameters.append(review_status)
        rows = connection.execute(
            f"SELECT * FROM ontology_object_type WHERE {' AND '.join(where)} ORDER BY kind,object_type",
            parameters,
        ).fetchall()
        return {"schemaVersion": "ontology-runtime-v1", "items": [dict(row) for row in rows], "sourceWrite": False, "formalPublication": False}
    finally:
        connection.close()


def ontology_relation_types(review_status: str = "all") -> dict[str, Any]:
    connection = unified_semantics_connection()
    try:
        where = ["status='active'"]
        parameters: list[Any] = []
        if review_status and review_status != "all":
            where.append("review_status=?")
            parameters.append(review_status)
        rows = connection.execute(
            f"SELECT * FROM ontology_relation_type WHERE {' AND '.join(where)} ORDER BY review_status,predicate",
            parameters,
        ).fetchall()
        return {"schemaVersion": "ontology-runtime-v1", "items": [dict(row) for row in rows], "sourceWrite": False, "formalPublication": False}
    finally:
        connection.close()


def ontology_event_types(review_status: str = "all") -> dict[str, Any]:
    connection = unified_semantics_connection()
    try:
        where = ["status='active'"]
        parameters: list[Any] = []
        if review_status and review_status != "all":
            where.append("review_status=?")
            parameters.append(review_status)
        rows = connection.execute(
            f"SELECT * FROM ontology_event_type WHERE {' AND '.join(where)} ORDER BY review_status,event_type",
            parameters,
        ).fetchall()
        return {"schemaVersion": "ontology-runtime-v1", "items": [dict(row) for row in rows], "sourceWrite": False, "formalPublication": False}
    finally:
        connection.close()



def build_router() -> APIRouter:
    router = APIRouter()
    router.add_api_route("/api/ontology/meta-summary", ontology_meta_summary, methods=["GET"])
    router.add_api_route("/api/ontology/object-types", ontology_object_types, methods=["GET"])
    router.add_api_route("/api/ontology/relation-types", ontology_relation_types, methods=["GET"])
    router.add_api_route("/api/ontology/event-types", ontology_event_types, methods=["GET"])
    return router
