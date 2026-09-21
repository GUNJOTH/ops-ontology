"""数据测试：app.core.idempotency.find_audit_event_by_idempotency。"""
from __future__ import annotations

import json

import pytest
from app.core.idempotency import find_audit_event_by_idempotency

pytestmark = pytest.mark.unit


def _seed(connection, event_type: str, entity_id: str, payload: dict) -> None:
    connection.execute(
        "INSERT INTO audit_event(entity_type,entity_id,event_type,actor,payload_json,event_at) "
        "VALUES (?,?,?,?,?,?)",
        ("x", entity_id, event_type, "a", json.dumps(payload, ensure_ascii=False), "t"),
    )
    connection.commit()


def test_find_by_idempotency_key(audit_db) -> None:
    _seed(audit_db, "approval", "e1", {"idempotency_key": "key-1"})
    _seed(audit_db, "approval", "e2", {"idempotency_key": "key-2"})
    row = find_audit_event_by_idempotency(
        audit_db, event_type="approval", idempotency_key="key-2"
    )
    assert row is not None
    assert row["entity_id"] == "e2"


def test_find_returns_none_when_no_match(audit_db) -> None:
    _seed(audit_db, "approval", "e1", {"idempotency_key": "key-1"})
    assert (
        find_audit_event_by_idempotency(
            audit_db, event_type="approval", idempotency_key="missing"
        )
        is None
    )


def test_find_filters_by_event_type(audit_db) -> None:
    _seed(audit_db, "approval", "e1", {"idempotency_key": "key-1"})
    assert (
        find_audit_event_by_idempotency(
            audit_db, event_type="other", idempotency_key="key-1"
        )
        is None
    )


def test_find_skips_malformed_payload(audit_db) -> None:
    audit_db.execute(
        "INSERT INTO audit_event(entity_type,entity_id,event_type,actor,payload_json,event_at) "
        "VALUES ('x','e1','approval','a','not-json','t')"
    )
    audit_db.commit()
    assert (
        find_audit_event_by_idempotency(
            audit_db, event_type="approval", idempotency_key="whatever"
        )
        is None
    )


def test_find_respects_limit(audit_db) -> None:
    for i in range(5):
        _seed(audit_db, "approval", f"e{i}", {"idempotency_key": f"key-{i}"})
    # 只回看最近 2 条：最早的 key-0 不可见，最新的 key-4 可见。
    assert (
        find_audit_event_by_idempotency(
            audit_db, event_type="approval", idempotency_key="key-0", limit=2
        )
        is None
    )
    assert (
        find_audit_event_by_idempotency(
            audit_db, event_type="approval", idempotency_key="key-4", limit=2
        )
        is not None
    )
