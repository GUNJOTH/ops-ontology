"""写路径集成测试：POST /api/cleaning/rules（写事务 + 审计 + 冲突回滚）。

口径：monkeypatch ``app.core.db.SQLITE_DB`` 到临时库，绝不触碰 ``system/data``。
"""
from __future__ import annotations

import json
import sqlite3

import pytest

pytestmark = pytest.mark.integration


BODY = {
    "ruleKey": "rule-trim-extra",
    "cleaningType": "trim",
    "ruleLabel": "去除首尾空格",
    "actionLabel": "清洗",
    "isCleaning": True,
    "replayId": "replay-trim-extra",
    "ruleVersion": "v1",
}


def _open(db_path):
    connection = sqlite3.connect(str(db_path))
    connection.row_factory = sqlite3.Row
    return connection


def test_register_cleaning_rule_end_to_end(client, isolated_workflow_db) -> None:
    response = client.post("/api/cleaning/rules", json=BODY)
    assert response.status_code == 200, response.text
    assert response.json() == {
        "ruleKey": "rule-trim-extra",
        "replayId": "replay-trim-extra",
        "status": "registered",
    }

    connection = _open(isolated_workflow_db)
    try:
        rule = connection.execute(
            "SELECT * FROM cleaning_rule_registry WHERE rule_key=?", (BODY["ruleKey"],)
        ).fetchone()
        assert rule is not None
        assert rule["enabled"] == 0

        run = connection.execute(
            "SELECT * FROM cleaning_run WHERE rule_key=?", (BODY["ruleKey"],)
        ).fetchone()
        assert run is not None
        assert run["status"] == "draft"

        audit = connection.execute(
            "SELECT payload_json FROM audit_event "
            "WHERE event_type='cleaning_rule_registered' AND entity_id=?",
            (BODY["ruleKey"],),
        ).fetchone()
        assert audit is not None
        assert json.loads(audit["payload_json"])["ruleKey"] == BODY["ruleKey"]
    finally:
        connection.close()


def test_register_cleaning_rule_conflict_rolls_back(client, isolated_workflow_db) -> None:
    first = client.post("/api/cleaning/rules", json=BODY)
    assert first.status_code == 200

    second = client.post("/api/cleaning/rules", json=BODY)
    assert second.status_code == 409

    connection = _open(isolated_workflow_db)
    try:
        rules = connection.execute(
            "SELECT count(*) AS c FROM cleaning_rule_registry WHERE rule_key=?", (BODY["ruleKey"],)
        ).fetchone()["c"]
        runs = connection.execute(
            "SELECT count(*) AS c FROM cleaning_run WHERE rule_key=?", (BODY["ruleKey"],)
        ).fetchone()["c"]
        audits = connection.execute(
            "SELECT count(*) AS c FROM audit_event "
            "WHERE event_type='cleaning_rule_registered' AND entity_id=?",
            (BODY["ruleKey"],),
        ).fetchone()["c"]
        assert rules == 1
        assert runs == 1
        assert audits == 1  # 冲突路径整体回滚，不残留审计
    finally:
        connection.close()
