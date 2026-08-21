"""Build source-fact pointers and derivation ledgers for explainable semantics.

Only accepted source-event references are materialized as observed facts.  No
numeric observation, defect, work-order or escalation fact is invented when a
source field is absent.
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
IDENTITY_RESULT_ROOT = ROOT.parent / "pilots" / "identity" / "results"


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sid(prefix: str, *parts: object) -> str:
    raw = "|".join("" if part is None else str(part) for part in parts)
    return f"{prefix}-{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:24]}"


def latest_identity_db() -> pathlib.Path | None:
    databases = sorted(IDENTITY_RESULT_ROOT.glob("identity-layer-v1-*/identity_semantics.sqlite3"), reverse=True)
    return databases[0] if databases else None


def load_source_events() -> dict[tuple[str, str, str], dict[str, object]]:
    """Read event detail from the local identity snapshot without modifying it."""
    path = latest_identity_db()
    if path is None:
        return {}
    source = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True, timeout=30)
    source.row_factory = sqlite3.Row
    try:
        return {
            (row["source_schema"], row["source_table"], row["source_row_id"]): {
                "event_record_id": row["event_record_id"],
                "event_type": row["event_type"],
                "source_table": row["source_table"],
                "source_row_id": row["source_row_id"],
                "site_id": row["site_id"],
                "location_code": row["location_code"],
                "event_time": row["event_time"],
                "status": row["status"],
                "description": row["description"],
                "link_status": row["link_status"],
            }
            for row in source.execute("SELECT * FROM device_event").fetchall()
        }
    finally:
        source.close()


def build(target_path: pathlib.Path) -> dict[str, object]:
    db = sqlite3.connect(str(target_path), timeout=30)
    db.row_factory = sqlite3.Row
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS semantic_fact (
          fact_id TEXT PRIMARY KEY,
          fact_type TEXT NOT NULL,
          subject_type TEXT NOT NULL,
          subject_key TEXT NOT NULL,
          predicate TEXT NOT NULL,
          value_json TEXT NOT NULL,
          unit TEXT,
          source_schema TEXT NOT NULL,
          source_table TEXT NOT NULL,
          source_row_id TEXT NOT NULL,
          source_snapshot_id TEXT,
          status TEXT NOT NULL CHECK (status IN ('observed','derived','accepted','retracted')),
          confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
          observed_at TEXT,
          created_at TEXT NOT NULL,
          UNIQUE(fact_type,subject_key,predicate,source_schema,source_table,source_row_id)
        );
        CREATE TABLE IF NOT EXISTS semantic_fact_derivation (
          derivation_id TEXT PRIMARY KEY,
          output_fact_id TEXT NOT NULL REFERENCES semantic_fact(fact_id),
          rule_asset_id TEXT NOT NULL,
          rule_version_id TEXT NOT NULL,
          input_fact_ids_json TEXT NOT NULL,
          input_context_json TEXT NOT NULL,
          decision_id TEXT,
          explanation TEXT NOT NULL,
          constraint_results_json TEXT NOT NULL,
          status TEXT NOT NULL CHECK (status IN ('proposed','accepted','rejected','blocked')),
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS semantic_rule_decision (
          decision_id TEXT PRIMARY KEY,
          subject_type TEXT NOT NULL,
          subject_key TEXT NOT NULL,
          rule_asset_id TEXT NOT NULL,
          rule_version_id TEXT NOT NULL,
          input_fact_ids_json TEXT NOT NULL,
          input_context_json TEXT NOT NULL,
          decision TEXT NOT NULL,
          confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
          explanation TEXT NOT NULL,
          status TEXT NOT NULL CHECK (status IN ('proposed','accepted','rejected','needs_review')),
          requires_action INTEGER NOT NULL CHECK (requires_action IN (0,1)),
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS semantic_escalation_action (
          action_id TEXT PRIMARY KEY,
          decision_id TEXT NOT NULL REFERENCES semantic_rule_decision(decision_id),
          action_type TEXT NOT NULL CHECK (action_type IN ('escalate','create_work_order','create_resolution_event','notify')),
          target_type TEXT NOT NULL,
          target_key TEXT,
          payload_json TEXT NOT NULL,
          status TEXT NOT NULL CHECK (status IN ('planned','approved','executed','blocked')),
          requires_approval INTEGER NOT NULL CHECK (requires_approval IN (0,1)),
          idempotency_key TEXT NOT NULL UNIQUE,
          source_write INTEGER NOT NULL CHECK (source_write=0),
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS semantic_fact_layer_run (
          run_id TEXT PRIMARY KEY,
          observed_fact_count INTEGER NOT NULL,
          derived_fact_count INTEGER NOT NULL,
          decision_count INTEGER NOT NULL,
          action_count INTEGER NOT NULL,
          source_write INTEGER NOT NULL CHECK (source_write=0),
          formal_publication INTEGER NOT NULL CHECK (formal_publication=0),
          created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_semantic_fact_subject ON semantic_fact(subject_type,subject_key,status);
        CREATE INDEX IF NOT EXISTS ix_semantic_fact_derivation_output ON semantic_fact_derivation(output_fact_id,status);
        CREATE INDEX IF NOT EXISTS ix_semantic_rule_decision_subject ON semantic_rule_decision(subject_type,subject_key,status);
        CREATE INDEX IF NOT EXISTS ix_semantic_escalation_status ON semantic_escalation_action(status,created_at);
        """
    )
    created = now()
    event_types = {
        "inspection": "observation_event",
        "defect": "defect_event",
        "work_order": "work_order_event",
    }
    source_events = load_source_events()
    relations = db.execute(
        """
        SELECT subject_type,subject_key,object_key,source_schema,source_table,source_row_id,
          source_snapshot_id,evidence_json,confidence
        FROM business_object_relation
        WHERE predicate='recorded_for' AND status='accepted' AND object_type='device' AND object_key IS NOT NULL
        """
    ).fetchall()
    for row in relations:
        fact_type = event_types.get(row["subject_type"], "business_event")
        fact_id = sid("FACT", fact_type, row["subject_key"], row["object_key"])
        source_event = source_events.get((row["source_schema"], row["source_table"], row["source_row_id"]))
        value = {
            "event_key": row["subject_key"],
            "source_row_id": row["source_row_id"],
            "provenance_only": True,
            "source_event": source_event,
            "note": "字段来自只读身份快照，未推断数值观测、缺陷等级或处理结论",
        }
        db.execute(
            """
            INSERT INTO semantic_fact(fact_id,fact_type,subject_type,subject_key,predicate,value_json,unit,
              source_schema,source_table,source_row_id,source_snapshot_id,status,confidence,observed_at,created_at)
            VALUES (?,?,?,?,?,?,NULL,?,?,?,?, 'observed',?,?,?)
            ON CONFLICT(fact_id) DO UPDATE SET
              value_json=excluded.value_json,
              source_snapshot_id=excluded.source_snapshot_id,
              confidence=excluded.confidence,
              observed_at=excluded.observed_at,
              created_at=excluded.created_at
            """,
            (fact_id, fact_type, "device", row["object_key"], "has_event", json.dumps(value, ensure_ascii=False),
             row["source_schema"], row["source_table"], row["source_row_id"], row["source_snapshot_id"],
             row["confidence"], None, created),
        )
    run_id = sid("SFL", created)
    counts = (
        int(db.execute("SELECT count(*) FROM semantic_fact WHERE status='observed'").fetchone()[0]),
        int(db.execute("SELECT count(*) FROM semantic_fact WHERE status='derived'").fetchone()[0]),
        int(db.execute("SELECT count(*) FROM semantic_rule_decision").fetchone()[0]),
        int(db.execute("SELECT count(*) FROM semantic_escalation_action").fetchone()[0]),
    )
    db.execute(
        """INSERT INTO semantic_fact_layer_run(run_id,observed_fact_count,derived_fact_count,decision_count,action_count,
          source_write,formal_publication,created_at) VALUES (?,?,?,?,?,0,0,?)""",
        (run_id, *counts, created),
    )
    db.commit()
    db.close()
    return {"run_id": run_id, "observed_fact_count": counts[0], "derived_fact_count": counts[1], "decision_count": counts[2], "action_count": counts[3], "source_write": False, "formal_publication": False}


def main() -> None:
    parser = argparse.ArgumentParser(description="Build explainable semantic fact pointers and derivation ledgers")
    parser.add_argument("--target-db", type=pathlib.Path, default=DEFAULT_TARGET)
    args = parser.parse_args()
    print(json.dumps(build(args.target_db.resolve()), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
