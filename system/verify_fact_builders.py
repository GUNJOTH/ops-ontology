"""Verify deterministic Fact Builder outputs on a temporary local overlay."""
from __future__ import annotations

import json
import pathlib
import shutil
import sqlite3

from pipeline.contracts import connect_local

ROOT = pathlib.Path(__file__).resolve().parent
SOURCE_DB = ROOT / "data" / "unified_semantics.sqlite3"
VERIFY_DIR = ROOT / "data" / ".verification"


def insert_event_fact(db: sqlite3.Connection, fact_id: str, fact_type: str, subject_key: str, event: dict[str, object], event_time: str) -> None:
    db.execute(
        """INSERT INTO semantic_fact(
          fact_id,fact_type,subject_type,subject_key,predicate,value_json,unit,
          source_schema,source_table,source_row_id,source_snapshot_id,status,
          confidence,observed_at,created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (fact_id, fact_type, "device", subject_key, "has_event", json.dumps({"source_event": event}, ensure_ascii=False), None,
         "FIXTURE", fact_type.upper(), fact_id, "fixture-fact-builder", "observed", 1.0, event_time, event_time),
    )


def verify() -> dict[str, object]:
    VERIFY_DIR.mkdir(parents=True, exist_ok=True)
    target = VERIFY_DIR / "fact_builders.test.sqlite3"
    target.unlink(missing_ok=True)
    shutil.copy2(SOURCE_DB, target)
    db = connect_local(target)
    try:
        insert_event_fact(db, "FIXTURE-OBS-1", "observation_event", "FIXTURE-DEVICE-FB", {"event_type": "inspection", "event_time": "2024-01-01T00:00:00+00:00", "temperature": 91, "unit": "C"}, "2024-01-01T00:00:00+00:00")
        insert_event_fact(db, "FIXTURE-OBS-2", "observation_event", "FIXTURE-DEVICE-FB", {"event_type": "inspection", "event_time": "2024-01-02T00:00:00+00:00", "temperature": 95, "unit": "C"}, "2024-01-02T00:00:00+00:00")
        insert_event_fact(db, "FIXTURE-DEF-1", "defect_event", "FIXTURE-DEVICE-FB", {"event_type": "defect", "event_time": "2024-01-01T01:00:00+00:00", "description": "轴承温度高", "severity": "high"}, "2024-01-01T01:00:00+00:00")
        insert_event_fact(db, "FIXTURE-DEF-2", "defect_event", "FIXTURE-DEVICE-FB", {"event_type": "defect", "event_time": "2024-01-10T01:00:00+00:00", "description": "轴承温度高", "severity": "high"}, "2024-01-10T01:00:00+00:00")
        db.commit()
    finally:
        db.close()

    import build_semantic_fact_builders as builder
    result = builder.build(target)
    db = connect_local(target)
    try:
        counts = dict(db.execute("SELECT fact_type,count(*) FROM semantic_fact WHERE source_table='semantic_fact_builder' GROUP BY fact_type").fetchall())
        risk_values = [json.loads(row[0]) for row in db.execute("SELECT value_json FROM semantic_fact WHERE fact_type='risk_assessment' AND source_table='semantic_fact_builder'").fetchall()]
        missing_derived_snapshots = int(db.execute("SELECT count(*) FROM semantic_fact WHERE source_table='semantic_fact_builder' AND status='derived' AND (source_snapshot_id IS NULL OR trim(source_snapshot_id)='')").fetchone()[0])
        derivation_count = int(db.execute("SELECT count(*) FROM semantic_fact_derivation WHERE rule_asset_id LIKE 'SBR:%'").fetchone()[0])
    finally:
        db.close()
        target.unlink(missing_ok=True)

    assert result["status"] == "completed", result
    assert result["measurement"] == 2, result
    assert result["history_stat"] == 1, result
    assert result["trend"] == 1, result
    assert result["repeated_defect"] == 1, result
    assert result["severe_defect"] == 2, result
    assert result["risk_assessment"] == 1, result
    assert counts["risk_assessment"] == 1, counts
    assert risk_values[0]["risk_score"] >= 4, risk_values
    assert missing_derived_snapshots == 0, missing_derived_snapshots
    assert derivation_count >= 8, derivation_count
    return {"status": "passed", "builder_result": result, "fact_counts": counts, "risk_score": risk_values[0]["risk_score"], "missing_derived_snapshots": missing_derived_snapshots, "derivation_count": derivation_count, "source_write": False, "formal_publication": False}


if __name__ == "__main__":
    print(json.dumps(verify(), ensure_ascii=False, indent=2))