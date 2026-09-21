"""Unified device query routes (native APIRouter)."""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query

from app.core.db import identity_result_connection, unified_semantics_connection

from .service import unified_device_list_row


def unified_device_summary() -> dict[str, Any]:
    identity = identity_result_connection()
    overlay = unified_semantics_connection()
    try:
        run = overlay.execute(
            "SELECT * FROM semantic_layer_run ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        if run is None:
            raise HTTPException(status_code=503, detail="统一设备语义覆盖层没有运行批次")
        map_counts = {
            row["status"]: int(row["count"])
            for row in identity.execute(
                "SELECT status,count(*) AS count FROM device_identity_map GROUP BY status"
            ).fetchall()
        }
        relation_counts = {
            row["status"]: int(row["count"])
            for row in overlay.execute(
                "SELECT status,count(*) AS count FROM unified_device_relation GROUP BY status"
            ).fetchall()
        }
        link_counts: dict[str, dict[str, int]] = {}
        for row in overlay.execute(
            "SELECT business_type,status,count(*) AS count FROM business_record_link GROUP BY business_type,status"
        ).fetchall():
            link_counts.setdefault(row["business_type"], {})[row["status"]] = int(row["count"])
        event_counts: dict[str, dict[str, int]] = {}
        for row in identity.execute(
            "SELECT event_type,link_status,count(*) AS count FROM device_event GROUP BY event_type,link_status"
        ).fetchall():
            event_counts.setdefault(row["event_type"], {})[row["link_status"]] = int(row["count"])
        source_systems = [dict(row) for row in overlay.execute(
            "SELECT system_key,display_name,connection_kind,snapshot_id,read_only,status FROM source_system ORDER BY system_key"
        ).fetchall()]
        return {
            "runId": run["run_id"],
            "sourceSnapshotId": run["source_snapshot_id"],
            "identityDatabase": run["identity_db_path"],
            "unifiedDeviceCount": int(run["unified_device_count"]),
            "identityMapCount": int(run["identity_map_count"]),
            "identityMapByStatus": map_counts,
            "relationCount": int(run["relation_count"]),
            "relationByStatus": relation_counts,
            "businessLinkCount": int(run["business_link_count"]),
            "acceptedBusinessLinkCount": int(run["accepted_business_link_count"]),
            "businessLinksByType": link_counts,
            "businessEventCount": int(identity.execute("SELECT count(*) FROM device_event").fetchone()[0]),
            "businessEventsByTypeStatus": event_counts,
            "crossSystemCandidateCount": int(identity.execute("SELECT count(*) FROM cross_system_match_candidate").fetchone()[0]),
            "sourceSystems": source_systems,
            "sourceWrite": False,
            "formalPublication": False,
            "policy": [
                "源系统和源表只读",
                "统一设备 ID 独立于 HD/XNY 的源编码",
                "跨系统映射不自动合并，必须保留证据和审核状态",
                "巡检、缺陷、工单仅通过本地业务记录挂接表关联",
            ],
        }
    finally:
        overlay.close()
        identity.close()


def unified_devices(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    search: str | None = None,
    source_schema: Literal["all", "HD_SAAS", "XNY_SAAS"] = "all",
    site_id: str | None = None,
) -> dict[str, Any]:
    identity = identity_result_connection()
    overlay = unified_semantics_connection()
    try:
        where: list[str] = []
        parameters: list[Any] = []
        if source_schema != "all":
            where.append("master_source_schema=?")
            parameters.append(source_schema)
        if site_id:
            where.append("site_id=?")
            parameters.append(site_id)
        if search and search.strip():
            value = f"%{search.strip()}%"
            where.append("(asset_number LIKE ? OR canonical_name LIKE ? OR location_code LIKE ? OR source_asset_id LIKE ?)")
            parameters.extend([value] * 4)
        where_sql = f"WHERE {' AND '.join(where)}" if where else ""
        total = int(identity.execute(f"SELECT count(*) FROM unified_device {where_sql}", parameters).fetchone()[0])
        rows = identity.execute(
            f"""
            SELECT unified_device_id,master_source_schema,site_id,asset_number,source_asset_id,
              canonical_name,location_code,parent_asset_number,org_id,classification_id,status,
              seed_status,source_snapshot_id
            FROM unified_device
            {where_sql}
            ORDER BY master_source_schema,site_id,asset_number,unified_device_id
            LIMIT ? OFFSET ?
            """,
            parameters + [page_size, (page - 1) * page_size],
        ).fetchall()
        if not rows:
            return {"rows": [], "total": total, "page": page, "pageSize": page_size}
        ids = [row["unified_device_id"] for row in rows]
        marks = ",".join("?" for _ in ids)
        map_counts = {
            row["unified_device_id"]: int(row["count"])
            for row in identity.execute(
                f"SELECT unified_device_id,count(*) AS count FROM device_identity_map WHERE unified_device_id IN ({marks}) GROUP BY unified_device_id",
                ids,
            ).fetchall()
        }
        link_counts = {
            row["unified_device_id"]: int(row["count"])
            for row in overlay.execute(
                f"SELECT unified_device_id,count(*) AS count FROM business_record_link WHERE unified_device_id IN ({marks}) GROUP BY unified_device_id",
                ids,
            ).fetchall()
        }
        accepted_link_counts = {
            row["unified_device_id"]: int(row["count"])
            for row in overlay.execute(
                f"SELECT unified_device_id,count(*) AS count FROM business_record_link WHERE status='accepted' AND unified_device_id IN ({marks}) GROUP BY unified_device_id",
                ids,
            ).fetchall()
        }
        review_link_counts = {
            row["unified_device_id"]: int(row["count"])
            for row in overlay.execute(
                f"SELECT unified_device_id,count(*) AS count FROM business_record_link WHERE status IN ('needs_review','blocked') AND unified_device_id IN ({marks}) GROUP BY unified_device_id",
                ids,
            ).fetchall()
        }
        relation_counts = {
            row["unified_device_id"]: int(row["count"])
            for row in overlay.execute(
                f"SELECT subject_unified_device_id AS unified_device_id,count(*) AS count FROM unified_device_relation WHERE subject_unified_device_id IN ({marks}) GROUP BY subject_unified_device_id",
                ids,
            ).fetchall()
        }
        return {
            "rows": [unified_device_list_row(row, map_counts.get(row["unified_device_id"], 0), link_counts.get(row["unified_device_id"], 0), relation_counts.get(row["unified_device_id"], 0), accepted_link_counts.get(row["unified_device_id"], 0), review_link_counts.get(row["unified_device_id"], 0)) for row in rows],
            "total": total,
            "page": page,
            "pageSize": page_size,
        }
    finally:
        overlay.close()
        identity.close()


def unified_device_detail(unified_device_id: str) -> dict[str, Any]:
    identity = identity_result_connection()
    overlay = unified_semantics_connection()
    try:
        row = identity.execute(
            "SELECT * FROM unified_device WHERE unified_device_id=?",
            (unified_device_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="统一设备对象不存在")
        mappings = [dict(item) for item in identity.execute(
            """
            SELECT source_schema,source_table_group,source_table,source_row_id,source_key_type,
              source_key,site_id,raw_description,location_code,match_method,match_confidence,
              status,evidence_json,source_snapshot_id
            FROM device_identity_map WHERE unified_device_id=? ORDER BY source_schema,source_table_group,source_table,source_row_id
            """,
            (unified_device_id,),
        ).fetchall()]
        links = [dict(item) for item in overlay.execute(
            """
            SELECT link_id,source_schema,source_table_group,source_table,source_row_id,business_type,
              source_key_type,source_key,status,confidence,evidence_json,source_snapshot_id
            FROM business_record_link WHERE unified_device_id=? ORDER BY business_type,source_schema,source_table,source_row_id
            """,
            (unified_device_id,),
        ).fetchall()]
        relations = [dict(item) for item in overlay.execute(
            """
            SELECT relation_id,subject_unified_device_id,predicate,object_unified_device_id,
              source_schema,source_table,source_row_id,confidence,status,evidence_json,source_snapshot_id
            FROM unified_device_relation
            WHERE subject_unified_device_id=? OR object_unified_device_id=?
            ORDER BY predicate,relation_id
            """,
            (unified_device_id, unified_device_id),
        ).fetchall()]
        object_ids = sorted({item["object_unified_device_id"] for item in relations if item["object_unified_device_id"]})
        related: list[dict[str, Any]] = []
        if object_ids:
            marks = ",".join("?" for _ in object_ids)
            related = [dict(item) for item in identity.execute(
                f"SELECT unified_device_id,master_source_schema,site_id,asset_number,canonical_name FROM unified_device WHERE unified_device_id IN ({marks})",
                object_ids,
            ).fetchall()]
        location: dict[str, Any] | None = None
        location_hierarchy: list[dict[str, Any]] = []
        location_code = (row["location_code"] or "").strip()
        if location_code:
            location_row = identity.execute(
                """
                SELECT * FROM function_location
                WHERE source_schema=? AND site_id=? AND location_code=?
                ORDER BY changed_at DESC, location_record_id
                LIMIT 1
                """,
                (row["master_source_schema"], row["site_id"], location_code),
            ).fetchone()
            if location_row:
                location = {
                    "locationRecordId": location_row["location_record_id"],
                    "sourceSchema": location_row["source_schema"],
                    "siteId": location_row["site_id"],
                    "locationCode": location_row["location_code"],
                    "sourceLocationId": location_row["source_location_id"] or "",
                    "description": location_row["description"] or "",
                    "parentLocation": location_row["parent_location"] or "",
                    "status": location_row["status"] or "",
                    "classificationId": location_row["classstructure_id"] or "",
                    "sourceSnapshotId": location_row["source_snapshot_id"],
                }
            location_hierarchy = [dict(item) for item in identity.execute(
                """
                SELECT hierarchy_record_id,source_schema,site_id,location_code,parent_location,
                  source_hierarchy_id,org_id,source_snapshot_id
                FROM location_hierarchy
                WHERE source_schema=? AND site_id=? AND location_code=?
                ORDER BY hierarchy_record_id
                """,
                (row["master_source_schema"], row["site_id"], location_code),
            ).fetchall()]
        business_record_evidence = [dict(item) for item in identity.execute(
            """
            SELECT event_record_id,unified_device_id,source_schema,event_type,source_table,source_row_id,
              site_id,location_code,event_time,status,description,source_snapshot_id,link_status,evidence_json
            FROM device_event
            WHERE unified_device_id=?
            ORDER BY event_type,event_record_id
            """,
            (unified_device_id,),
        ).fetchall()]
        return {
            "device": unified_device_list_row(
                row,
                len(mappings),
                len(links),
                len(relations),
                sum(1 for item in links if item["status"] == "accepted"),
                sum(1 for item in links if item["status"] in {"needs_review", "blocked"}),
            ),
            "sourceIdentity": {
                "sourceIdentityKey": row["source_identity_key"],
                "sourceAssetId": row["source_asset_id"] or "",
                "masterSourceSchema": row["master_source_schema"],
            },
            "mappings": mappings,
            "businessLinks": links,
            "relations": relations,
            "relatedDevices": related,
            "location": location,
            "locationHierarchy": location_hierarchy,
            "businessRecordEvidence": business_record_evidence,
            "sourceWrite": False,
            "formalPublication": False,
        }
    finally:
        overlay.close()
        identity.close()



def build_router() -> APIRouter:
    router = APIRouter()
    router.add_api_route("/api/unified-devices/summary", unified_device_summary, methods=["GET"])
    router.add_api_route("/api/unified-devices", unified_devices, methods=["GET"])
    router.add_api_route("/api/unified-devices/{unified_device_id}", unified_device_detail, methods=["GET"])
    return router
