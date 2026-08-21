"""Activate the confirmed space rules after the sample and replay gates pass."""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DB = ROOT / "data" / "semantic_workflow.sqlite3"
PREVIEW_DIR = ROOT.parent / "pilots" / "HD_SAAS" / "space_rule_preview"
CONFIRMATION_MANIFEST = PREVIEW_DIR / "confirmation_manifest.json"
REPLAY_MANIFEST = PREVIEW_DIR / "replay_manifest.json"
RULE_VERSION = "space-normalization-proposed-20260812-v1"
VALIDATOR_VERSION = "hd-semantic-validator-0.2.0"
ACTOR = "local-user-confirmed-space-rules"

RULES = (
    {
        "rule_key": "format.fullwidth_space_to_ascii",
        "source_term": "\u3000",
        "target_term": " ",
        "guardrail": "只将全角空格转换为半角空格，不改动非空白字符",
    },
    {
        "rule_key": "format.collapse_repeated_ascii_space",
        "source_term": "  ",
        "target_term": " ",
        "guardrail": "仅将连续两个及以上半角空格压缩为一个半角空格",
    },
    {
        "rule_key": "format.trim_description_space",
        "source_term": "首尾空白",
        "target_term": "去除首尾空白",
        "guardrail": "只清理描述首尾空白，不改动中间内容",
    },
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def main() -> None:
    confirmation = json.loads(CONFIRMATION_MANIFEST.read_text(encoding="utf-8"))
    replay = json.loads(REPLAY_MANIFEST.read_text(encoding="utf-8"))
    if confirmation.get("status") != "confirmed_for_replay" or confirmation.get("selected_count") != 200:
        raise SystemExit("The 200-row sample confirmation gate is not satisfied.")
    if replay.get("status") != "passed" or replay.get("evaluation_count") != 500 or replay.get("pass_count") != 500 or replay.get("fail_count") != 0:
        raise SystemExit("The 500-case replay gate is not satisfied.")
    if replay.get("rule_version") != RULE_VERSION:
        raise SystemExit("Replay rule version does not match activation rule version.")
    if confirmation.get("source_write") is not False or confirmation.get("formal_publication") is not False:
        raise SystemExit("Sample confirmation is not read-only.")

    connection = sqlite3.connect(DB, timeout=60)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=60000")
    connection.execute("PRAGMA foreign_keys=ON")
    now = utc_now()
    activation_id = f"space-rule-activation-{uuid.uuid4().hex}"
    replay_id = replay["replay_id"]
    confirmation_id = confirmation["confirmation_id"]
    try:
        connection.execute("BEGIN IMMEDIATE")
        existing_by_key = {
            row["rule_key"]: row
            for row in connection.execute(
                "SELECT * FROM terminology_rule WHERE rule_key IN (?,?,?)",
                tuple(rule["rule_key"] for rule in RULES),
            ).fetchall()
        }
        for rule in RULES:
            existing = existing_by_key.get(rule["rule_key"])
            if existing is not None:
                if existing["version"] != RULE_VERSION or existing["rule_type"] != "format":
                    raise SystemExit(f"Existing rule conflicts with requested version: {rule['rule_key']}")
                connection.execute(
                    """
                    UPDATE terminology_rule
                    SET source_term=?,target_term=?,status='active',version=?,evidence_json=?,
                        confirmed_by=?,confirmed_at=?,updated_at=?
                    WHERE rule_key=?
                    """,
                    (
                        rule["source_term"],
                        rule["target_term"],
                        RULE_VERSION,
                        json.dumps({
                            "confirmation_id": confirmation_id,
                            "replay_id": replay_id,
                            "sample_count": 200,
                            "replay_count": 500,
                            "guardrail": rule["guardrail"],
                        }, ensure_ascii=False),
                        ACTOR,
                        now,
                        now,
                        rule["rule_key"],
                    ),
                )
            else:
                connection.execute(
                    """
                    INSERT INTO terminology_rule
                      (term_rule_id,rule_key,rule_type,source_term,target_term,
                       site_scope,classification_scope,context_condition_json,priority,
                       status,version,evidence_json,confirmed_by,confirmed_at,created_at,updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        f"term-{rule['rule_key'].replace('.', '-')}",
                        rule["rule_key"],
                        "format",
                        rule["source_term"],
                        rule["target_term"],
                        None,
                        None,
                        json.dumps({"field": "DESCRIPTION", "guardrail": rule["guardrail"]}, ensure_ascii=False),
                        50,
                        "active",
                        RULE_VERSION,
                        json.dumps({
                            "confirmation_id": confirmation_id,
                            "replay_id": replay_id,
                            "sample_count": 200,
                            "replay_count": 500,
                            "guardrail": rule["guardrail"],
                        }, ensure_ascii=False),
                        ACTOR,
                        now,
                        now,
                        now,
                    ),
                )

        payload = {
            "activation_id": activation_id,
            "rule_keys": [rule["rule_key"] for rule in RULES],
            "rule_version": RULE_VERSION,
            "validator_version": VALIDATOR_VERSION,
            "confirmation_id": confirmation_id,
            "sample_count": 200,
            "replay_id": replay_id,
            "replay_count": 500,
            "replay_pass_count": 500,
            "replay_fail_count": 0,
            "source_write": False,
            "formal_publication": False,
        }
        connection.execute(
            "INSERT INTO audit_event(entity_type,entity_id,event_type,actor,payload_json,event_at) VALUES (?,?,?,?,?,?)",
            ("terminology_rule", activation_id, "space_rules_activated", ACTOR, json.dumps(payload, ensure_ascii=False), now),
        )
        connection.commit()
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        connection.close()
        raise
    connection.close()

    manifest = {
        **payload,
        "status": "active",
        "activated_at_utc": now,
    }
    (PREVIEW_DIR / "activation_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False))


if __name__ == "__main__":
    main()
