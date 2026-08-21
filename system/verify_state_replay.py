"""Verify deterministic state replay, guards, subject scope, and late events."""
from __future__ import annotations

import json
import pathlib
import shutil
import sqlite3
import sys

ROOT = pathlib.Path(__file__).resolve().parent
SOURCE_DB = ROOT / "data" / "unified_semantics.sqlite3"
VERIFY_DIR = ROOT / "data" / ".verification"


def insert_fact(db: sqlite3.Connection, fact_id: str, subject_key: str, state: str, event_type: str, event_time: str, recorded_at: str, **context: object) -> None:
    source_id = f"SRC-{fact_id}"
    source_value = {"source_event": {"event_type": event_type, "event_time": event_time, "status": event_type, **context}}
    db.execute(
        """INSERT INTO semantic_fact(
          fact_id,fact_type,subject_type,subject_key,predicate,value_json,unit,
          source_schema,source_table,source_row_id,source_snapshot_id,status,
          confidence,observed_at,created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (source_id, "defect_event", "device", subject_key, "source_event", json.dumps(source_value, ensure_ascii=False), None,
         "FIXTURE", "DEFECT_EVENT", source_id, "fixture-snapshot", "observed", 1.0, event_time, recorded_at),
    )
    canonical_value = {"canonical_state": state, "raw_status": state, "derived_from_fact_id": source_id}
    db.execute(
        """INSERT INTO semantic_fact(
          fact_id,fact_type,subject_type,subject_key,predicate,value_json,unit,
          source_schema,source_table,source_row_id,source_snapshot_id,status,
          confidence,observed_at,created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (fact_id, "canonical_defect_state", "device", subject_key, "has_canonical_defect_state", json.dumps(canonical_value), None,
         "LOCAL_SEMANTIC", "fixture_status", fact_id, "fixture-snapshot", "derived", 1.0, event_time, recorded_at),
    )


def verify() -> dict[str, object]:
    if not SOURCE_DB.exists():
        raise AssertionError(f"missing source database: {SOURCE_DB}")
    VERIFY_DIR.mkdir(parents=True, exist_ok=True)
    target = VERIFY_DIR / "state_replay.test.sqlite3"
    target.unlink(missing_ok=True)
    shutil.copy2(SOURCE_DB, target)
    sys.path.insert(0, str(ROOT))
    import execute_state_transitions as replay_runtime

    subject = "FIXTURE-DEVICE-001"
    db = sqlite3.connect(str(target))
    try:
        # Insert in reverse time order to prove replay does not use fact_id order.
        insert_fact(db, "FIXTURE-CLOSED", subject, "CLOSED", "defect_acceptance", "2024-01-05T00:00:00+00:00", "2024-01-05T00:01:00+00:00", acceptance_pass=True)
        insert_fact(db, "FIXTURE-RESOLVED", subject, "RESOLVED", "resolution", "2024-01-04T00:00:00+00:00", "2024-01-04T00:01:00+00:00", resolution_id="RES-001")
        insert_fact(db, "FIXTURE-PROCESSING", subject, "PROCESSING", "defect_processing", "2024-01-03T00:00:00+00:00", "2024-01-03T00:01:00+00:00", team_num="TEAM-001")
        insert_fact(db, "FIXTURE-PENDING", subject, "PENDING", "defect_accepted", "2024-01-02T00:00:00+00:00", "2024-01-02T00:01:00+00:00")
        insert_fact(db, "FIXTURE-NEW", subject, "NEW", "defect_created", "2024-01-01T00:00:00+00:00", "2024-01-01T00:01:00+00:00")
        # A later-recorded event with an earlier effective time is retained and flagged.
        insert_fact(db, "FIXTURE-LATE", subject, "PENDING", "defect_accepted", "2024-01-01T12:00:00+00:00", "2024-02-01T00:00:00+00:00")
        db.commit()
    finally:
        db.close()

    result = replay_runtime.execute(target, "device", subject)
    db = sqlite3.connect(str(target))
    try:
        states = db.execute(
            "SELECT current_state FROM semantic_current_state WHERE subject_type='device' AND subject_key=? AND state_domain='DEFECT'",
            (subject,),
        ).fetchall()
        transitions = db.execute(
            "SELECT to_state,status,late_arrival,order_key FROM semantic_state_transition WHERE subject_key=? ORDER BY order_key",
            (subject,),
        ).fetchall()
        invalid = db.execute(
            "SELECT count(*) FROM semantic_state_transition WHERE subject_key=? AND status='needs_review'",
            (subject,),
        ).fetchone()[0]
        scoped_other = db.execute(
            "SELECT count(*) FROM semantic_state_transition WHERE subject_key<>? AND replay_run_id=?",
            (subject, result["run_id"]),
        ).fetchone()[0]
    finally:
        db.close()

    assert result["status"] == "needs_review", result
    assert result["lateArrivalCount"] == 1, result
    assert states == [("CLOSED",)], states
    accepted_states = [row[0] for row in transitions if row[1] == "accepted"]
    assert accepted_states == ["NEW", "PENDING", "PROCESSING", "RESOLVED", "CLOSED"], transitions
    assert invalid == 1, transitions
    assert scoped_other == 0, scoped_other

    # An approved canonical snapshot without a typed transition event may
    # safely seed the initial state when it has a source time and mapping
    # evidence.  This is the formal-snapshot case used by the current local
    # overlay; it must not be mistaken for an arbitrary status transition.
    snapshot_subject = "FIXTURE-SNAPSHOT-001"
    db = sqlite3.connect(str(target))
    try:
        insert_fact(db, "FIXTURE-SNAPSHOT-CLOSED", snapshot_subject, "CLOSED", "defect", "2024-02-01T00:00:00+00:00", "2024-02-01T00:01:00+00:00")
        db.execute(
            "UPDATE semantic_fact SET value_json=? WHERE fact_id=?",
            (json.dumps({"canonical_state": "CLOSED", "raw_status": "CLOSED", "derived_from_fact_id": "SRC-FIXTURE-SNAPSHOT-CLOSED", "dictionary_status_id": "SDS-FIXTURE", "mapping_version": 1}), "FIXTURE-SNAPSHOT-CLOSED"),
        )
        db.commit()
    finally:
        db.close()
    snapshot_result = replay_runtime.execute(target, "device", snapshot_subject)
    db = sqlite3.connect(str(target))
    try:
        snapshot_state = db.execute(
            "SELECT current_state,status FROM semantic_current_state WHERE subject_type='device' AND subject_key=? AND state_domain='DEFECT'",
            (snapshot_subject,),
        ).fetchone()
        snapshot_reason = db.execute(
            "SELECT json_extract(evidence_json,'$.reason_code') FROM semantic_state_transition WHERE subject_key=?",
            (snapshot_subject,),
        ).fetchone()[0]
    finally:
        db.close()
        target.unlink(missing_ok=True)
    assert snapshot_result["status"] == "completed", snapshot_result
    assert snapshot_state == ("CLOSED", "current"), snapshot_state
    assert snapshot_reason == "approved_snapshot_seed", snapshot_reason
    return {
        "status": "passed",
        "replay_status": result["status"],
        "current_state": "CLOSED",
        "ordered_accepted_transitions": accepted_states,
        "late_arrival_count": result["lateArrivalCount"],
        "review_transition_count": invalid,
        "difference_count": result["differenceCount"],
        "snapshot_seed_status": snapshot_result["status"],
        "source_write": False,
        "formal_publication": False,
    }


if __name__ == "__main__":
    print(json.dumps(verify(), ensure_ascii=False, indent=2))
