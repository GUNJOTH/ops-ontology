"""Project accepted source-event facts into one normalized semantic event layer."""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import sqlite3
from datetime import datetime, timezone

from pipeline.contracts import connect_local

ROOT = pathlib.Path(__file__).resolve().parent
DEFAULT_TARGET = ROOT / "data" / "unified_semantics.sqlite3"

EVENT_TYPES = {
    "observation_event": "InspectionEvent",
    "defect_event": "DefectEvent",
    "work_order_event": "WorkOrderEvent",
}

EVENT_TYPE_VALUES = (
    "InspectionEvent", "AbnormalInspectionEvent", "DefectEvent", "DefectCreatedEvent",
    "DefectAcceptedEvent", "DefectProcessingEvent", "ResolutionEvent",
    "DefectAcceptanceEvent", "WorkOrderEvent", "WorkPermitEvent", "HumanReviewEvent",
    "BusinessEvent",
)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sid(prefix: str, *parts: object) -> str:
    raw = "|".join("" if part is None else str(part) for part in parts)
    return f"{prefix}-{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:24]}"


def ensure_schema(db: sqlite3.Connection) -> None:
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS semantic_event (
          event_id TEXT PRIMARY KEY,
          event_type TEXT NOT NULL CHECK (event_type IN ('InspectionEvent','AbnormalInspectionEvent','DefectEvent','DefectCreatedEvent','DefectAcceptedEvent','DefectProcessingEvent','ResolutionEvent','DefectAcceptanceEvent','WorkOrderEvent','WorkPermitEvent','HumanReviewEvent','BusinessEvent')),
          subject_type TEXT NOT NULL,
          subject_key TEXT NOT NULL,
          source_event_id TEXT,
          source_schema TEXT NOT NULL,
          source_table TEXT NOT NULL,
          source_row_id TEXT NOT NULL,
          source_snapshot_id TEXT NOT NULL,
          site_id TEXT,
          location_code TEXT,
          occurred_at TEXT,
          recorded_at TEXT,
          raw_status TEXT,
          description TEXT,
          identity_status TEXT NOT NULL CHECK (identity_status IN ('accepted','needs_review','unknown')),
          payload_json TEXT NOT NULL,
          confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
          status TEXT NOT NULL CHECK (status IN ('observed','accepted','needs_review','retracted')),
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          UNIQUE(event_type,source_schema,source_table,source_row_id,source_snapshot_id)
        );
        CREATE TABLE IF NOT EXISTS semantic_event_layer_run (
          run_id TEXT PRIMARY KEY,
          input_fact_count INTEGER NOT NULL,
          accepted_event_count INTEGER NOT NULL,
          review_event_count INTEGER NOT NULL,
          event_type_counts_json TEXT NOT NULL,
          source_write INTEGER NOT NULL CHECK (source_write=0),
          formal_publication INTEGER NOT NULL CHECK (formal_publication=0),
          created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_semantic_event_subject
          ON semantic_event(subject_type,subject_key,occurred_at);
        CREATE INDEX IF NOT EXISTS ix_semantic_event_type_time
          ON semantic_event(event_type,occurred_at,status);
        CREATE INDEX IF NOT EXISTS ix_semantic_event_source
          ON semantic_event(source_schema,source_table,source_row_id);
        """
    )
    schema_row = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='semantic_event'").fetchone()
    schema_sql = str(schema_row[0] or "") if schema_row else ""
    if "DefectCreatedEvent" not in schema_sql or "HumanReviewEvent" not in schema_sql:
        db.execute("DROP INDEX IF EXISTS ix_semantic_event_subject")
        db.execute("DROP INDEX IF EXISTS ix_semantic_event_type_time")
        db.execute("DROP INDEX IF EXISTS ix_semantic_event_source")
        db.execute("ALTER TABLE semantic_event RENAME TO semantic_event_legacy")
        db.execute(
            """CREATE TABLE semantic_event (
              event_id TEXT PRIMARY KEY,
              event_type TEXT NOT NULL CHECK (event_type IN ('InspectionEvent','AbnormalInspectionEvent','DefectEvent','DefectCreatedEvent','DefectAcceptedEvent','DefectProcessingEvent','ResolutionEvent','DefectAcceptanceEvent','WorkOrderEvent','WorkPermitEvent','HumanReviewEvent','BusinessEvent')),
              subject_type TEXT NOT NULL, subject_key TEXT NOT NULL, source_event_id TEXT,
              source_schema TEXT NOT NULL, source_table TEXT NOT NULL, source_row_id TEXT NOT NULL,
              source_snapshot_id TEXT NOT NULL, site_id TEXT, location_code TEXT, occurred_at TEXT,
              recorded_at TEXT, raw_status TEXT, description TEXT, identity_status TEXT NOT NULL,
              payload_json TEXT NOT NULL, confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
              status TEXT NOT NULL CHECK (status IN ('observed','accepted','needs_review','retracted')),
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
              UNIQUE(event_type,source_schema,source_table,source_row_id,source_snapshot_id)
            )"""
        )
        db.execute(
            """INSERT INTO semantic_event(
              event_id,event_type,subject_type,subject_key,source_event_id,source_schema,source_table,
              source_row_id,source_snapshot_id,site_id,location_code,occurred_at,recorded_at,raw_status,
              description,identity_status,payload_json,confidence,status,created_at,updated_at
            ) SELECT event_id,event_type,subject_type,subject_key,source_event_id,source_schema,source_table,
              source_row_id,source_snapshot_id,site_id,location_code,occurred_at,recorded_at,raw_status,
              description,identity_status,payload_json,confidence,status,created_at,updated_at
              FROM semantic_event_legacy"""
        )
        db.execute("DROP TABLE semantic_event_legacy")
        db.execute("CREATE INDEX IF NOT EXISTS ix_semantic_event_subject ON semantic_event(subject_type,subject_key,occurred_at)")
        db.execute("CREATE INDEX IF NOT EXISTS ix_semantic_event_type_time ON semantic_event(event_type,occurred_at,status)")
        db.execute("CREATE INDEX IF NOT EXISTS ix_semantic_event_source ON semantic_event(source_schema,source_table,source_row_id)")


def build(target_path: pathlib.Path) -> dict[str, object]:
    db = connect_local(target_path, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA busy_timeout=30000")
    ensure_schema(db)
    created = now()
    facts = db.execute(
        """
        SELECT * FROM semantic_fact
        WHERE fact_type IN ('observation_event','defect_event','work_order_event')
          AND status IN ('observed','accepted')
        ORDER BY fact_id
        """
    ).fetchall()
    accepted = 0
    review = 0
    counts: dict[str, int] = {}
    for fact in facts:
        try:
            value = json.loads(fact["value_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            value = {}
        if not isinstance(value, dict):
            value = {}
        source_event = value.get("source_event") if isinstance(value.get("source_event"), dict) else {}
        event_type = EVENT_TYPES.get(fact["fact_type"], "BusinessEvent")
        identity_status = str(source_event.get("link_status") or "unknown")
        if identity_status not in {"accepted", "needs_review", "unknown"}:
            identity_status = "unknown"
        event_status = "accepted" if identity_status == "accepted" else "needs_review"
        accepted += int(event_status == "accepted")
        review += int(event_status == "needs_review")
        counts[event_type] = counts.get(event_type, 0) + 1
        source_event_id = source_event.get("event_record_id")
        event_id = sid("EVENT", event_type, fact["source_schema"], fact["source_table"], fact["source_row_id"], fact["source_snapshot_id"])
        payload = {
            "fact_id": fact["fact_id"],
            "event_key": value.get("event_key"),
            "source_event": source_event,
            "provenance_only": value.get("provenance_only", True),
        }
        db.execute(
            """
            INSERT INTO semantic_event(
              event_id,event_type,subject_type,subject_key,source_event_id,
              source_schema,source_table,source_row_id,source_snapshot_id,
              site_id,location_code,occurred_at,recorded_at,raw_status,description,
              identity_status,payload_json,confidence,status,created_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(event_id) DO UPDATE SET
              source_event_id=excluded.source_event_id,
              site_id=excluded.site_id,
              location_code=excluded.location_code,
              occurred_at=excluded.occurred_at,
              recorded_at=excluded.recorded_at,
              raw_status=excluded.raw_status,
              description=excluded.description,
              identity_status=excluded.identity_status,
              payload_json=excluded.payload_json,
              confidence=excluded.confidence,
              status=excluded.status,
              updated_at=excluded.updated_at
            """,
            (
                event_id, event_type, fact["subject_type"], fact["subject_key"], source_event_id,
                fact["source_schema"], fact["source_table"], fact["source_row_id"], fact["source_snapshot_id"],
                source_event.get("site_id"), source_event.get("location_code"), source_event.get("event_time"),
                source_event.get("recorded_at") or source_event.get("recordedAt"), source_event.get("status"), source_event.get("description"), identity_status,
                json.dumps(payload, ensure_ascii=False), fact["confidence"], event_status, created, created,
            ),
        )
    run_id = sid("SELR", created)
    db.execute(
        """
        INSERT INTO semantic_event_layer_run(
          run_id,input_fact_count,accepted_event_count,review_event_count,
          event_type_counts_json,source_write,formal_publication,created_at
        ) VALUES (?,?,?,?,?,?,?,?)
        """,
        (run_id, len(facts), accepted, review, json.dumps(counts, ensure_ascii=False), 0, 0, created),
    )
    db.commit()
    db.close()
    return {
        "run_id": run_id,
        "input_fact_count": len(facts),
        "accepted_event_count": accepted,
        "review_event_count": review,
        "event_type_counts": counts,
        "source_write": False,
        "formal_publication": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the local semantic event layer")
    parser.add_argument("--target-db", type=pathlib.Path, default=DEFAULT_TARGET)
    args = parser.parse_args()
    print(json.dumps(build(args.target_db.resolve()), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
