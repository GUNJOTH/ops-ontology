"""Published-result query and export handlers.

This module owns the published description read/export surface previously embedded in app.main.
"""
from __future__ import annotations

import csv
import io
import sys
from typing import Any

from app.core.config import (
    DEPENDENCY_DIR,
)

if DEPENDENCY_DIR.exists():
    sys.path.insert(0, str(DEPENDENCY_DIR))

from fastapi import HTTPException, Query
from fastapi.responses import Response

from app.core.db import sqlite_connection
from app.core.utils import parse_json_array
from app.domains.candidates.serializers import PUBLISHED_SELECT, row_to_publication


def published_filters(
    search: str | None,
    site_id: str | None,
    rule: str | None,
) -> tuple[str, list[Any]]:
    where: list[str] = []
    parameters: list[Any] = []
    if search and search.strip():
        # Keep search tolerant of source padding while preserving exact
        # asset-number and KKS lookup semantics.
        value = f"%{search.strip()}%"
        where.append(
            "(TRIM(p.asset_number) LIKE ? OR TRIM(p.final_description) LIKE ? "
            "OR TRIM(c.original_description) LIKE ? OR TRIM(d.location_code) LIKE ? "
            "OR TRIM(d.location_description) LIKE ?)"
        )
        parameters.extend([value] * 5)
    if site_id:
        where.append("p.site_id = ?")
        parameters.append(site_id)
    if rule:
        if rule == "format.fullwidth_parenthesis_to_ascii":
            where.append("p.final_description != c.original_description AND (c.original_description LIKE ? OR c.original_description LIKE ?)")
            parameters.extend(["%（%", "%）%"])
        elif rule == "format.fullwidth_comma_to_ascii":
            where.append("p.final_description != c.original_description AND c.original_description LIKE ?")
            parameters.append("%，%")
        else:
            where.append("c.applied_rule_ids_json LIKE ?")
            parameters.append(f"%{rule}%")
    return (" WHERE " + " AND ".join(where)) if where else "", parameters


def published(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    search: str | None = None,
    site_id: str | None = None,
    rule: str | None = None,
) -> dict[str, Any]:
    sqlite = sqlite_connection()
    try:
        where_sql, parameters = published_filters(search, site_id, rule)
        total = sqlite.execute(
            f"SELECT count(*) {PUBLISHED_SELECT[PUBLISHED_SELECT.index('FROM '):]}{where_sql}",
            parameters,
        ).fetchone()[0]
        rows = sqlite.execute(
            f"{PUBLISHED_SELECT}{where_sql} ORDER BY p.site_id,p.asset_number,p.publication_id LIMIT ? OFFSET ?",
            [*parameters, page_size, (page - 1) * page_size],
        ).fetchall()
        sites = sqlite.execute(
            "SELECT site_id,count(*) AS count FROM published_description GROUP BY site_id ORDER BY count DESC,site_id"
        ).fetchall()
        rules = sqlite.execute(
            """
            SELECT c.applied_rule_ids_json,count(*) AS row_count
            FROM semantic_candidate c
            JOIN published_description p ON p.candidate_id=c.candidate_id
            GROUP BY c.applied_rule_ids_json
            """
        ).fetchall()
        rule_counts: dict[str, int] = {}
        for row in rules:
            rule_keys = parse_json_array(row["applied_rule_ids_json"])
            for rule_key in set(rule_keys):
                rule_counts[rule_key] = rule_counts.get(rule_key, 0) + int(row["row_count"])
        return {
            "rows": [row_to_publication(row) for row in rows],
            "total": int(total),
            "page": page,
            "pageSize": page_size,
            "sites": [{"siteId": row["site_id"], "count": int(row["count"])} for row in sites],
            "rules": [{"rule": key, "count": count} for key, count in sorted(rule_counts.items())],
        }
    finally:
        sqlite.close()


def published_export_before_detail(
    search: str | None = None,
    site_id: str | None = None,
    rule: str | None = None,
) -> Response:
    """Keep the static export route ahead of the publication-id route."""
    return published_export(search=search, site_id=site_id, rule=rule)


def published_detail(publication_id: str) -> dict[str, Any]:
    sqlite = sqlite_connection()
    try:
        row = sqlite.execute(
            f"{PUBLISHED_SELECT} WHERE p.publication_id=?",
            (publication_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="正式结果不存在")
        return row_to_publication(row)
    finally:
        sqlite.close()


def published_export(
    search: str | None = None,
    site_id: str | None = None,
    rule: str | None = None,
) -> Response:
    sqlite = sqlite_connection()
    try:
        where_sql, parameters = published_filters(search, site_id, rule)
        rows = sqlite.execute(
            f"{PUBLISHED_SELECT}{where_sql} ORDER BY p.site_id,p.asset_number,p.publication_id",
            parameters,
        ).fetchall()
    finally:
        sqlite.close()

    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(["SITEID", "ASSETNUM", "原始描述", "正式统一描述", "KKS", "位置", "分类", "规则版本", "发布批次", "发布时间"])
    writer.writerows([
        [row["site_id"], row["asset_number"], row["original_description"] or "", row["final_description"],
         row["location_code"] or "", row["location_description"] or "", row["classification_description"] or "",
         row["rule_version"], row["batch_id"], row["published_at"]]
        for row in rows
    ])
    return Response(
        content="\ufeff" + output.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="published_descriptions.csv"'},
    )
