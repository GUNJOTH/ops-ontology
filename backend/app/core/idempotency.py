"""Idempotency lookups for local approval and governance actions."""
from __future__ import annotations

import json
import sqlite3
from typing import Any


def find_audit_event_by_idempotency(
    connection: sqlite3.Connection,
    *,
    event_type: str,
    idempotency_key: str,
    limit: int = 1000,
) -> sqlite3.Row | None:
    """Find an earlier audit event carrying the same caller idempotency key."""
    rows = connection.execute(
        "SELECT entity_id,payload_json FROM audit_event WHERE event_type=? ORDER BY event_id DESC LIMIT ?",
        (event_type, limit),
    ).fetchall()
    for row in rows:
        try:
            payload: Any = json.loads(row["payload_json"])
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict) and payload.get("idempotency_key") == idempotency_key:
            return row
    return None

