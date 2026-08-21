"""Execute conservative, deterministic business-fact rules locally.

The first rules intentionally derive only event-presence facts from already
accepted device-event evidence.  They do not infer measurements, severity,
or work-order actions, and never write an upstream source system.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import sqlite3
from datetime import datetime, timezone


ROOT = pathlib.Path(__file__).resolve().parent
DEFAULT_TARGET = ROOT / "data" / "unified_semantics.sqlite3"


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sid(prefix: str, *parts: object) -> str:
    raw = "|".join("" if part is None else str(part) for part in parts)
    return f"{prefix}-{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:24]}"


# Bootstrap entries for the local registry.  The executor below reads the
# enabled registry rows back from SQLite, so later rule versions can be added
# without changing this execution loop.
DEFAULT_RULES: tuple[dict[str, object], ...] = (
    {
        "rule_asset_id": "SBR:event-presence.inspection",
        "rule_version_id": "SBRV:event-presence.inspection-v1",
        "input_fact_type": "observation_event",
        "output_fact_type": "observation_presence",
        "output_predicate": "has_observation_evidence",
        "decision": "observation_event_present",
        "explanation": "已确认该设备存在来源可追溯的巡检事件；未推断巡检数值或异常结论。",
    },
    {
        "rule_asset_id": "SBR:event-presence.defect",
        "rule_version_id": "SBRV:event-presence.defect-v1",
        "input_fact_type": "defect_event",
        "output_fact_type": "defect_presence",
        "output_predicate": "has_defect_evidence",
        "decision": "defect_event_present",
        "explanation": "已确认该设备存在来源可追溯的缺陷事件；未推断缺陷等级、是否未关闭或风险等级。",
    },
    {
        "rule_asset_id": "SBR:event-presence.work-order",
        "rule_version_id": "SBRV:event-presence.work-order-v1",
        "input_fact_type": "work_order_event",
        "output_fact_type": "work_order_presence",
        "output_predicate": "has_work_order_evidence",
        "decision": "work_order_event_present",
        "explanation": "已确认该设备存在来源可追溯的工单事件；未推断工单是否完成，也未创建新工单。",
    },
    {
        "rule_asset_id": "SBR:defect-status.presence",
        "rule_version_id": "SBRV:defect-status.presence-v1",
        "input_fact_type": "defect_status",
        "output_fact_type": "defect_status_presence",
        "output_predicate": "has_defect_status_evidence",
        "decision": "defect_status_present",
        "explanation": "缺陷事件存在来源可追溯的状态字段；当前只保留原始状态值，不解释其业务含义。",
    },
)


def ensure_schema(db: sqlite3.Connection) -> None:
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS semantic_logic_rule (
          rule_asset_id TEXT PRIMARY KEY,
          rule_version_id TEXT NOT NULL,
          input_fact_type TEXT NOT NULL,
          input_predicate TEXT NOT NULL DEFAULT 'has_event',
          output_fact_type TEXT NOT NULL,
          output_predicate TEXT NOT NULL,
          decision TEXT NOT NULL,
          explanation TEXT NOT NULL,
          status TEXT NOT NULL CHECK (status IN ('enabled','disabled','blocked')),
          requires_approval INTEGER NOT NULL CHECK (requires_approval IN (0,1)),
          source_write INTEGER NOT NULL CHECK (source_write=0),
          created_at TEXT NOT NULL,
          UNIQUE(rule_asset_id,rule_version_id)
        );
        CREATE TABLE IF NOT EXISTS semantic_reasoning_run (
          run_id TEXT PRIMARY KEY,
          input_fact_count INTEGER NOT NULL,
          matched_rule_count INTEGER NOT NULL,
          decision_count INTEGER NOT NULL,
          derived_fact_count INTEGER NOT NULL,
          action_count INTEGER NOT NULL,
          status TEXT NOT NULL CHECK (status IN ('completed','needs_review','blocked')),
          source_write INTEGER NOT NULL CHECK (source_write=0),
          formal_publication INTEGER NOT NULL CHECK (formal_publication=0),
          created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_semantic_logic_rule_input
          ON semantic_logic_rule(input_fact_type,status);
        CREATE INDEX IF NOT EXISTS ix_semantic_reasoning_run_created
          ON semantic_reasoning_run(created_at);
        """
    )


def materialize_defect_status_facts(db: sqlite3.Connection, created: str) -> int:
    """Split raw defect status into explicit, provenance-preserving facts."""
    source_facts = db.execute(
        """
        SELECT * FROM semantic_fact
        WHERE fact_type='defect_event' AND status IN ('observed','accepted') AND predicate='has_event'
        ORDER BY fact_id
        """
    ).fetchall()
    created_count = 0
    for fact in source_facts:
        try:
            value = json.loads(fact["value_json"])
        except (TypeError, json.JSONDecodeError):
            value = {}
        source_event = value.get("source_event") if isinstance(value, dict) else None
        if not isinstance(source_event, dict):
            source_event = {}
        raw_status = source_event.get("status")
        fact_id = sid("FACT", "defect_status", fact["subject_key"], fact["fact_id"])
        status_value = {
            "event_fact_id": fact["fact_id"],
            "event_record_id": source_event.get("event_record_id"),
            "raw_status": raw_status,
            "status_present": bool(str(raw_status or "").strip()),
            "provenance_only": True,
            "note": "只保留源状态原值，不解释编码或业务含义",
        }
        cursor = db.execute(
            """
            INSERT OR IGNORE INTO semantic_fact(
              fact_id,fact_type,subject_type,subject_key,predicate,value_json,unit,
              source_schema,source_table,source_row_id,source_snapshot_id,status,
              confidence,observed_at,created_at
            ) VALUES (?,?,?,?,?,?,NULL,?,?,?,?, 'observed',?,?,?)
            """,
            (
                fact_id, "defect_status", fact["subject_type"], fact["subject_key"], "has_status",
                json.dumps(status_value, ensure_ascii=False), fact["source_schema"], fact["source_table"],
                fact["source_row_id"], fact["source_snapshot_id"], fact["confidence"],
                None, created,
            ),
        )
        created_count += int(cursor.rowcount > 0)
    return created_count


def execute(target_path: pathlib.Path) -> dict[str, object]:
    db = sqlite3.connect(str(target_path), timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA busy_timeout=30000")
    ensure_schema(db)
    created = now()
    materialized_status_count = materialize_defect_status_facts(db, created)

    for rule in DEFAULT_RULES:
        db.execute(
            """
            INSERT INTO semantic_logic_rule(
              rule_asset_id,rule_version_id,input_fact_type,input_predicate,
              output_fact_type,output_predicate,decision,explanation,status,
              requires_approval,source_write,created_at
            ) VALUES (?,?,?,?,?,?,?,?, 'enabled',0,0,?)
            ON CONFLICT(rule_asset_id,rule_version_id) DO UPDATE SET
              status='enabled',explanation=excluded.explanation
            """,
            (
                rule["rule_asset_id"], rule["rule_version_id"], rule["input_fact_type"],
                "has_event", rule["output_fact_type"], rule["output_predicate"],
                rule["decision"], rule["explanation"], created,
            ),
        )

    logic_rules = [dict(row) for row in db.execute(
        "SELECT * FROM semantic_logic_rule WHERE status='enabled' ORDER BY rule_asset_id,rule_version_id"
    ).fetchall()]
    source_facts = db.execute(
        """
        SELECT * FROM semantic_fact
        WHERE status IN ('observed','accepted') AND predicate IN ('has_event','has_status')
        ORDER BY fact_id
        """
    ).fetchall()
    matched_rule_count = 0
    for fact in source_facts:
        rule = next((item for item in logic_rules if item["input_fact_type"] == fact["fact_type"]), None)
        if rule is None:
            continue
        matched_rule_count += 1
        decision_id = sid("SRD", rule["rule_version_id"], fact["fact_id"])
        derived_fact_id = sid("FACT", rule["output_fact_type"], fact["subject_key"], fact["fact_id"])
        derivation_id = sid("SFD", rule["rule_version_id"], fact["fact_id"])
        decision = rule["decision"]
        decision_status = "accepted"
        if rule["input_fact_type"] == "defect_status":
            try:
                status_value = json.loads(fact["value_json"])
            except (TypeError, json.JSONDecodeError):
                status_value = {}
            if not isinstance(status_value, dict) or not status_value.get("status_present"):
                decision = "defect_status_missing"
                decision_status = "needs_review"
        context = {
            "subject_type": fact["subject_type"],
            "subject_key": fact["subject_key"],
            "input_fact_type": fact["fact_type"],
            "source_only": True,
        }
        db.execute(
            """
            INSERT OR IGNORE INTO semantic_rule_decision(
              decision_id,subject_type,subject_key,rule_asset_id,rule_version_id,
              input_fact_ids_json,input_context_json,decision,confidence,explanation,
              status,requires_action,created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,0,?)
            """,
            (
                decision_id, fact["subject_type"], fact["subject_key"], rule["rule_asset_id"],
                rule["rule_version_id"], json.dumps([fact["fact_id"]]), json.dumps(context, ensure_ascii=False),
                decision, fact["confidence"], rule["explanation"], decision_status, created,
            ),
        )
        if decision_status != "accepted":
            continue
        derived_value = {
            "derived_from_fact_id": fact["fact_id"],
            "decision_id": decision_id,
            "rule_asset_id": rule["rule_asset_id"],
            "rule_version_id": rule["rule_version_id"],
            "evidence_only": True,
            "note": "仅表示事件存在性，不表示事件严重程度、数值阈值或处理状态",
        }
        db.execute(
            """
            INSERT OR IGNORE INTO semantic_fact(
              fact_id,fact_type,subject_type,subject_key,predicate,value_json,unit,
              source_schema,source_table,source_row_id,source_snapshot_id,status,
              confidence,observed_at,created_at
            ) VALUES (?,?,?,?,?,?,NULL,'LOCAL_SEMANTIC','semantic_rule_decision',?,?, 'derived',?,?,?)
            """,
            (
                derived_fact_id, rule["output_fact_type"], fact["subject_type"], fact["subject_key"],
                rule["output_predicate"], json.dumps(derived_value, ensure_ascii=False), decision_id,
                fact["source_snapshot_id"], fact["confidence"], None, created,
            ),
        )
        db.execute(
            """
            INSERT OR IGNORE INTO semantic_fact_derivation(
              derivation_id,output_fact_id,rule_asset_id,rule_version_id,
              input_fact_ids_json,input_context_json,decision_id,explanation,
              constraint_results_json,status,created_at
            ) VALUES (?,?,?,?,?,?,?,?,?, 'accepted',?)
            """,
            (
                derivation_id, derived_fact_id, rule["rule_asset_id"], rule["rule_version_id"],
                json.dumps([fact["fact_id"]]), json.dumps(context, ensure_ascii=False), decision_id,
                rule["explanation"], json.dumps({"source_write": "pass", "formal_publication": "pass"}), created,
            ),
        )

    counts = {
        "input_fact_count": len(source_facts),
        "matched_rule_count": matched_rule_count,
        "decision_count": int(db.execute("SELECT count(*) FROM semantic_rule_decision").fetchone()[0]),
        "derived_fact_count": int(db.execute("SELECT count(*) FROM semantic_fact WHERE status IN ('derived','accepted')").fetchone()[0]),
        "action_count": int(db.execute("SELECT count(*) FROM semantic_escalation_action").fetchone()[0]),
    }
    run_id = sid("SRL", created)
    db.execute(
        """
        INSERT INTO semantic_reasoning_run(
          run_id,input_fact_count,matched_rule_count,decision_count,derived_fact_count,
          action_count,status,source_write,formal_publication,created_at
        ) VALUES (?,?,?,?,?,?, 'completed',0,0,?)
        """,
        (run_id, counts["input_fact_count"], counts["matched_rule_count"], counts["decision_count"], counts["derived_fact_count"], counts["action_count"], created),
    )
    db.commit()
    db.close()
    return {"run_id": run_id, **counts, "materialized_status_count": materialized_status_count, "rules": logic_rules, "source_write": False, "formal_publication": False}


def main() -> None:
    parser = argparse.ArgumentParser(description="Execute conservative semantic rules against local observed facts")
    parser.add_argument("--target-db", type=pathlib.Path, default=DEFAULT_TARGET)
    args = parser.parse_args()
    print(json.dumps(execute(args.target_db.resolve()), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
