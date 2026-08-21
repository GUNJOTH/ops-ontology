"""Unified location payload helpers."""
from __future__ import annotations

import sqlite3
from typing import Any


def unified_location_row(row: sqlite3.Row, device_count: int, business_record_count: int) -> dict[str, Any]:
    return {
        "locationRecordId": row["location_record_id"],
        "sourceSchema": row["source_schema"],
        "siteId": row["site_id"],
        "locationCode": row["location_code"],
        "sourceLocationId": row["source_location_id"] or "",
        "description": row["description"] or "",
        "parentLocation": row["parent_location"] or "",
        "status": row["status"] or "",
        "classificationId": row["classstructure_id"] or "",
        "deviceCount": device_count,
        "businessRecordCount": business_record_count,
        "sourceSnapshotId": row["source_snapshot_id"],
    }

