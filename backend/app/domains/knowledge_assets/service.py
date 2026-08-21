"""Knowledge asset payload helpers."""
from __future__ import annotations

import sqlite3
from typing import Any


def knowledge_asset_row(row: sqlite3.Row, source_count: int, issue_count: int) -> dict[str, Any]:
    return {
        "assetId": row["asset_id"],
        "assetKey": row["asset_key"],
        "assetType": row["asset_type"],
        "title": row["title"],
        "canonicalDefinition": row["canonical_definition"],
        "currentVersion": row["current_version"],
        "status": row["status"],
        "sourceScope": row["source_scope"],
        "sourceCount": source_count,
        "issueCount": issue_count,
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }

