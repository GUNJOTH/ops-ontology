"""Unified device payload helpers."""
from __future__ import annotations

import sqlite3
from typing import Any


def unified_device_list_row(
    row: sqlite3.Row,
    mapping_count: int,
    link_count: int,
    relation_count: int,
    accepted_link_count: int = 0,
    review_link_count: int = 0,
) -> dict[str, Any]:
    return {
        "unifiedDeviceId": row["unified_device_id"],
        "sourceSchema": row["master_source_schema"],
        "siteId": row["site_id"],
        "assetNumber": row["asset_number"],
        "sourceAssetId": row["source_asset_id"] or "",
        "canonicalName": row["canonical_name"] or "",
        "locationCode": row["location_code"] or "",
        "parentAssetNumber": row["parent_asset_number"] or "",
        "organization": row["org_id"] or "",
        "classificationId": row["classification_id"] or "",
        "status": row["status"] or "",
        "seedStatus": row["seed_status"],
        "mappingCount": mapping_count,
        "businessLinkCount": link_count,
        "acceptedBusinessLinkCount": accepted_link_count,
        "businessReviewCount": review_link_count,
        "relationCount": relation_count,
        "mappingStatus": "已挂接业务记录" if accepted_link_count else ("业务记录待确认" if review_link_count else ("已有身份映射" if mapping_count else "仅设备主数据")),
        "sourceSnapshotId": row["source_snapshot_id"],
    }

