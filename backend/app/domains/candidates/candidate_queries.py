"""Candidate list/facet/detail query handlers.

This module owns the read-only candidate query surface.
"""
from __future__ import annotations

from typing import Any, Literal

from fastapi import HTTPException, Query

from app.core.db import sqlite_connection
from app.core.utils import parse_json_array

from .samples import DEFAULT_SAMPLE_TARGET, MAX_SAMPLE_TARGET, ensure_review_sample, latest_batch
from .serializers import row_to_candidate


def candidates(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    search: str | None = None,
    site_id: str | None = None,
    classification: str | None = None,
    quick_filter: Literal["all", "pending", "context", "low", "deferred"] = "all",
    sample_only: bool = False,
    sample_size: int = Query(default=DEFAULT_SAMPLE_TARGET, ge=1, le=MAX_SAMPLE_TARGET),
) -> dict[str, Any]:
    sqlite = sqlite_connection()
    try:
        batch = latest_batch(sqlite)
        where = ["c.batch_id = ?"]
        parameters: list[Any] = [batch["batch_id"]]
        if sample_only:
            sample = ensure_review_sample(sqlite, batch, sample_size)
            where.append("EXISTS (SELECT 1 FROM review_sample_item si WHERE si.sample_id=? AND si.candidate_id=c.candidate_id)")
            parameters.append(sample["sample_id"])
        if search and search.strip():
            value = f"%{search.strip()}%"
            where.append("(d.asset_number LIKE ? OR d.original_description LIKE ? OR c.candidate_description LIKE ? OR d.location_code LIKE ? OR d.location_description LIKE ?)")
            parameters.extend([value] * 5)
        if site_id:
            where.append("d.site_id = ?")
            parameters.append(site_id)
        if classification:
            where.append("d.classification_description = ?")
            parameters.append(classification)
        if quick_filter == "pending":
            where.append("c.review_state = 'pending'")
        elif quick_filter == "context":
            where.append("(c.reason_codes_json LIKE '%LOCATION%' OR c.reason_codes_json LIKE '%CONTEXT%')")
        elif quick_filter == "low":
            where.append("c.confidence = 'low'")
        elif quick_filter == "deferred":
            where.append("c.review_state = 'deferred'")
        where_sql = " AND ".join(where)
        total = sqlite.execute(
            f"SELECT count(*) FROM semantic_candidate c JOIN device_identity d ON d.device_id=c.device_id WHERE {where_sql}",
            parameters,
        ).fetchone()[0]
        parameters.extend([page_size, (page - 1) * page_size])
        rows = sqlite.execute(
            f"""
            SELECT c.candidate_id,c.batch_id,d.site_id,d.asset_number,c.original_description,
              c.candidate_description,d.location_code,d.location_description,d.location_parent,
              d.classification_description,c.confidence,c.validator_status,c.review_state,
              c.reason_codes_json,c.evidence_level,c.created_at
            FROM semantic_candidate c JOIN device_identity d ON d.device_id=c.device_id
            WHERE {where_sql}
            ORDER BY d.site_id, d.asset_number, c.candidate_id
            LIMIT ? OFFSET ?
            """,
            parameters,
        ).fetchall()
        return {"rows": [row_to_candidate(row) for row in rows], "total": int(total), "page": page, "pageSize": page_size}
    finally:
        sqlite.close()


def candidate_facets(
    quick_filter: Literal["all", "pending", "context", "low", "deferred"] = "all",
    sample_only: bool = False,
    sample_size: int = Query(default=DEFAULT_SAMPLE_TARGET, ge=1, le=MAX_SAMPLE_TARGET),
) -> dict[str, Any]:
    """Return filter values from the current local batch, never hard-coded UI values."""
    sqlite = sqlite_connection()
    try:
        batch = latest_batch(sqlite)
        where = ["c.batch_id=?"]
        parameters: list[Any] = [batch["batch_id"]]
        if sample_only:
            sample = ensure_review_sample(sqlite, batch, sample_size)
            where.append("EXISTS (SELECT 1 FROM review_sample_item si WHERE si.sample_id=? AND si.candidate_id=c.candidate_id)")
            parameters.append(sample["sample_id"])
        if quick_filter == "pending":
            where.append("c.review_state='pending'")
        elif quick_filter == "deferred":
            where.append("c.review_state='deferred'")
        elif quick_filter == "low":
            where.append("c.confidence='low'")
        elif quick_filter == "context":
            where.append("(c.reason_codes_json LIKE '%LOCATION%' OR c.reason_codes_json LIKE '%CONTEXT%')")
        where_sql = " AND ".join(where)
        sites = sqlite.execute(
            f"SELECT COALESCE(NULLIF(trim(d.site_id),''),'未填写') AS value,count(*) AS count FROM semantic_candidate c JOIN device_identity d ON d.device_id=c.device_id WHERE {where_sql} GROUP BY value ORDER BY count DESC,value",
            parameters,
        ).fetchall()
        classifications = sqlite.execute(
            f"SELECT COALESCE(NULLIF(trim(d.classification_description),''),'未分类') AS value,count(*) AS count FROM semantic_candidate c JOIN device_identity d ON d.device_id=c.device_id WHERE {where_sql} GROUP BY value ORDER BY count DESC,value LIMIT 50",
            parameters,
        ).fetchall()
        return {
            "batchId": batch["batch_id"],
            "sites": [{"value": row["value"], "count": int(row["count"])} for row in sites],
            "classifications": [{"value": row["value"], "count": int(row["count"])} for row in classifications],
            "sourceWrite": False,
            "formalPublication": False,
        }
    finally:
        sqlite.close()


def candidate_detail(candidate_id: str) -> dict[str, Any]:
    sqlite = sqlite_connection()
    try:
        row = sqlite.execute(
            """
            SELECT c.candidate_id,c.batch_id,c.original_description,c.candidate_description,c.confidence,
              c.validator_status,c.review_state,c.reason_codes_json,c.evidence_level,c.applied_rule_ids_json,
              c.rule_version,c.validator_version,c.created_at,d.source_asset_id,d.site_id,d.asset_number,
              d.source_row_hash,d.context_hash,d.location_code,d.location_description,d.location_parent,
              d.classification_description
            FROM semantic_candidate c JOIN device_identity d ON d.device_id=c.device_id
            WHERE c.candidate_id=?
            """,
            (candidate_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="候选记录不存在")
        result = row_to_candidate(row)
        result.update({
            "assetId": row["source_asset_id"] or "",
            "sourceRowHash": row["source_row_hash"],
            "contextHash": row["context_hash"],
            "specificationCount": 0,
            "featureCount": 0,
            "parentChildEvidence": "当前 SQLite 流程库只保存摘要；详细上下文从 DuckDB 分析库读取。",
            "appliedRules": parse_json_array(row["applied_rule_ids_json"]),
            "validatorVersion": row["validator_version"],
            "ruleVersion": row["rule_version"],
        })
        return result
    finally:
        sqlite.close()
