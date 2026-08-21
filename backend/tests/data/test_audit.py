"""数据测试：app.core.audit.append_audit_event。"""
from __future__ import annotations

import json

import pytest

from app.core.audit import append_audit_event

pytestmark = pytest.mark.unit


def test_append_audit_event_roundtrip(audit_db) -> None:
    append_audit_event(
        audit_db,
        entity_type="device",
        entity_id="d1",
        event_type="reviewed",
        actor="tester",
        payload={"decision": "accept", "sourceWrite": False},
        event_at="2026-01-01T00:00:00+00:00",
    )
    audit_db.commit()
    row = audit_db.execute("SELECT * FROM audit_event").fetchone()
    assert row["entity_type"] == "device"
    assert row["entity_id"] == "d1"
    assert row["event_type"] == "reviewed"
    assert row["actor"] == "tester"
    assert row["event_at"] == "2026-01-01T00:00:00+00:00"
    assert json.loads(row["payload_json"]) == {
        "decision": "accept",
        "sourceWrite": False,
    }


def test_append_audit_event_preserves_unicode_payload(audit_db) -> None:
    append_audit_event(
        audit_db,
        entity_type="x",
        entity_id="1",
        event_type="e",
        actor="a",
        payload={"note": "中文内容"},
        event_at="t",
    )
    audit_db.commit()
    row = audit_db.execute("SELECT payload_json FROM audit_event").fetchone()
    assert json.loads(row["payload_json"]) == {"note": "中文内容"}
