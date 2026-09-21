"""Unified location query routes (native APIRouter)."""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query

from app.core.db import identity_result_connection

from .service import unified_location_row


def unified_location_summary() -> dict[str, Any]:
    identity = identity_result_connection()
    try:
        source_counts = [
            {"sourceSchema": row["source_schema"], "count": int(row["count"])}
            for row in identity.execute(
                "SELECT source_schema,count(*) AS count FROM function_location GROUP BY source_schema ORDER BY source_schema"
            ).fetchall()
        ]
        hierarchy_counts = [
            {"sourceSchema": row["source_schema"], "count": int(row["count"])}
            for row in identity.execute(
                "SELECT source_schema,count(*) AS count FROM location_hierarchy GROUP BY source_schema ORDER BY source_schema"
            ).fetchall()
        ]
        return {
            "locationCount": int(identity.execute("SELECT count(*) FROM function_location").fetchone()[0]),
            "hierarchyCount": int(identity.execute("SELECT count(*) FROM location_hierarchy").fetchone()[0]),
            "sourceCounts": source_counts,
            "hierarchyBySource": hierarchy_counts,
            "sourceWrite": False,
            "formalPublication": False,
            "policy": [
                "位置对象来自源快照的 function_location 和 location_hierarchy",
                "设备只通过 source_schema + SITEID + LOCATION 关联位置",
                "位置层级缺失时保留空层级，不自动推断父级",
            ],
        }
    finally:
        identity.close()


def unified_locations(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    search: str | None = None,
    source_schema: Literal["all", "HD_SAAS", "XNY_SAAS"] = "all",
    site_id: str | None = None,
) -> dict[str, Any]:
    identity = identity_result_connection()
    try:
        where: list[str] = []
        parameters: list[Any] = []
        if source_schema != "all":
            where.append("source_schema=?")
            parameters.append(source_schema)
        if site_id and site_id.strip():
            where.append("site_id=?")
            parameters.append(site_id.strip())
        if search and search.strip():
            value = f"%{search.strip()}%"
            where.append("(location_code LIKE ? OR description LIKE ? OR parent_location LIKE ? OR source_location_id LIKE ?)")
            parameters.extend([value] * 4)
        where_sql = f"WHERE {' AND '.join(where)}" if where else ""
        total = int(identity.execute(f"SELECT count(*) FROM function_location {where_sql}", parameters).fetchone()[0])
        rows = identity.execute(
            f"""
            SELECT location_record_id,source_schema,site_id,location_code,source_location_id,
              description,parent_location,status,classstructure_id,source_snapshot_id
            FROM function_location
            {where_sql}
            ORDER BY source_schema,site_id,location_code,location_record_id
            LIMIT ? OFFSET ?
            """,
            parameters + [page_size, (page - 1) * page_size],
        ).fetchall()
        if not rows:
            return {"rows": [], "total": total, "page": page, "pageSize": page_size}
        keys = [(row["source_schema"], row["site_id"], row["location_code"]) for row in rows]
        device_counts: dict[tuple[str, str, str], int] = {}
        business_counts: dict[tuple[str, str, str], int] = {}
        for source, site, code in keys:
            key = (source, site, code)
            device_counts[key] = int(identity.execute(
                "SELECT count(*) FROM unified_device WHERE master_source_schema=? AND site_id=? AND location_code=?",
                key,
            ).fetchone()[0])
            business_counts[key] = int(identity.execute(
                "SELECT count(*) FROM device_event WHERE source_schema=? AND site_id=? AND location_code=?",
                key,
            ).fetchone()[0])
        return {
            "rows": [unified_location_row(row, device_counts.get((row["source_schema"], row["site_id"], row["location_code"]), 0), business_counts.get((row["source_schema"], row["site_id"], row["location_code"]), 0)) for row in rows],
            "total": total,
            "page": page,
            "pageSize": page_size,
            "sourceWrite": False,
            "formalPublication": False,
        }
    finally:
        identity.close()


def unified_location_detail(location_record_id: str) -> dict[str, Any]:
    identity = identity_result_connection()
    try:
        location = identity.execute(
            "SELECT * FROM function_location WHERE location_record_id=?",
            (location_record_id,),
        ).fetchone()
        if location is None:
            raise HTTPException(status_code=404, detail="统一位置对象不存在")
        hierarchy = [dict(row) for row in identity.execute(
            """
            SELECT hierarchy_record_id,source_schema,site_id,location_code,parent_location,
              source_hierarchy_id,org_id,source_snapshot_id
            FROM location_hierarchy
            WHERE source_schema=? AND site_id=? AND location_code=?
            ORDER BY hierarchy_record_id
            """,
            (location["source_schema"], location["site_id"], location["location_code"]),
        ).fetchall()]
        devices = [dict(row) for row in identity.execute(
            """
            SELECT unified_device_id,master_source_schema,site_id,asset_number,canonical_name,
              location_code,parent_asset_number,status
            FROM unified_device
            WHERE master_source_schema=? AND site_id=? AND location_code=?
            ORDER BY asset_number,unified_device_id
            LIMIT 200
            """,
            (location["source_schema"], location["site_id"], location["location_code"]),
        ).fetchall()]
        business_records = [dict(row) for row in identity.execute(
            """
            SELECT event_record_id,unified_device_id,event_type,source_table,source_row_id,
              site_id,location_code,event_time,status,description,link_status,evidence_json
            FROM device_event
            WHERE source_schema=? AND site_id=? AND location_code=?
            ORDER BY event_type,event_record_id
            LIMIT 200
            """,
            (location["source_schema"], location["site_id"], location["location_code"]),
        ).fetchall()]
        return {
            "location": unified_location_row(location, len(devices), len(business_records)),
            "hierarchy": hierarchy,
            "devices": devices,
            "businessRecords": business_records,
            "sourceWrite": False,
            "formalPublication": False,
        }
    finally:
        identity.close()



def build_router() -> APIRouter:
    router = APIRouter()
    router.add_api_route("/api/unified-locations/summary", unified_location_summary, methods=["GET"])
    router.add_api_route("/api/unified-locations", unified_locations, methods=["GET"])
    router.add_api_route("/api/unified-locations/{location_record_id}", unified_location_detail, methods=["GET"])
    return router
