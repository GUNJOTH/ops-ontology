"""Auditable local event writes; source systems are never touched here."""
from __future__ import annotations

import json
import sqlite3
from typing import Any


def append_audit_event(
    connection: sqlite3.Connection,
    *,
    entity_type: str,
    entity_id: str,
    event_type: str,
    actor: str,
    payload: dict[str, Any],
    event_at: str,
) -> None:
    connection.execute(
        "INSERT INTO audit_event(entity_type,entity_id,event_type,actor,payload_json,event_at) VALUES (?,?,?,?,?,?)",
        (entity_type, entity_id, event_type, actor, json.dumps(payload, ensure_ascii=False), event_at),
    )

