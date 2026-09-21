"""Dashboard and review-sample query handlers.

These handlers are HTTP-facing service functions owned by the dashboard domain.
"""
from __future__ import annotations

import sys
from typing import Any

from app.core.config import (
    DEPENDENCY_DIR,
)

if DEPENDENCY_DIR.exists():
    sys.path.insert(0, str(DEPENDENCY_DIR))

from fastapi import Query

from app.core.db import duckdb_connection, sqlite_connection
from app.domains.candidates.service import (
    DEFAULT_SAMPLE_TARGET,
    MAX_SAMPLE_TARGET,
    ensure_review_sample,
    latest_batch,
    review_sample_summary,
)


def dashboard() -> dict[str, Any]:
    sqlite = sqlite_connection()
    try:
        batch = latest_batch(sqlite)
        sample = ensure_review_sample(sqlite, batch)
        sample_info = review_sample_summary(sqlite, sample)
        source_snapshot_id = batch["source_snapshot_id"]
        batch_id = batch["batch_id"]
        counts = sqlite.execute(
            """
            SELECT
              count(*) AS total,
              sum(CASE WHEN validator_status = 'candidate' THEN 1 ELSE 0 END) AS candidate_count,
              sum(CASE WHEN review_state = 'pending' THEN 1 ELSE 0 END) AS pending_count,
              sum(CASE WHEN review_state IN ('approved','modified') THEN 1 ELSE 0 END) AS approved_count,
              sum(CASE WHEN review_state = 'rejected' THEN 1 ELSE 0 END) AS rejected_count,
              sum(CASE WHEN validator_status = 'blocked' THEN 1 ELSE 0 END) AS blocked_count
            FROM semantic_candidate WHERE batch_id=?
            """,
            (batch_id,),
        ).fetchone()
    finally:
        sqlite.close()

    duck = duckdb_connection()
    try:
        sites = duck.execute(
            """
            SELECT SITEID, count(*) AS device_count,
              round(count(*) * 100.0 / sum(count(*)) OVER (), 1) AS share
            FROM semantic_candidate_fact
            GROUP BY SITEID ORDER BY device_count DESC LIMIT 10
            """
        ).fetchall()
        coverage = duck.execute("SELECT * FROM v_context_coverage LIMIT 1").fetchone()
        coverage_columns = [item[0] for item in duck.description] if coverage is not None else []
    finally:
        duck.close()

    coverage_map = dict(zip(coverage_columns, coverage or []))
    row_count = int(coverage_map.get("row_count") or counts["total"] or 0)

    def percentage(key: str) -> float:
        return round((int(coverage_map.get(key) or 0) * 100.0 / row_count), 1) if row_count else 0

    return {
        "batchId": batch_id,
        "ruleVersion": batch["rule_version"],
        "sourceSnapshot": f"HD_SAAS / {source_snapshot_id[:12]}",
        "inputCount": int(batch["input_count"]),
        "candidateCount": int(counts["candidate_count"] or 0),
        "pendingReviewCount": int(counts["pending_count"] or 0),
        "approvedCount": int(counts["approved_count"] or 0),
        "publishedCount": int(batch["published_count"] or 0),
        "blockedCount": int(counts["blocked_count"] or 0),
        "readOnlySource": True,
        "samplesReady": int(sample_info["pendingCount"]),
        "reviewSample": sample_info,
        "sites": [{"siteId": row[0], "count": int(row[1]), "share": float(row[2])} for row in sites],
        "contextCoverage": [
            {"label": "KKS / 位置", "value": percentage("location_code_rows")},
            {"label": "位置父级", "value": percentage("location_parent_rows")},
            {"label": "分类", "value": percentage("classification_rows")},
            {"label": "规格 / 特征", "value": percentage("specification_rows")},
        ],
    }


def review_sample(sample_size: int = Query(default=DEFAULT_SAMPLE_TARGET, ge=1, le=MAX_SAMPLE_TARGET)) -> dict[str, Any]:
    sqlite = sqlite_connection()
    try:
        batch = latest_batch(sqlite)
        sample = ensure_review_sample(sqlite, batch, sample_size)
        return review_sample_summary(sqlite, sample)
    finally:
        sqlite.close()
