"""Exclude non-accepted identity candidates from the local processing pool.

This is a recoverable local-result-layer action.  It never deletes source rows,
never changes unified devices, and never publishes a formal semantic result.
Only exact accepted identity bridges remain eligible for downstream processing.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sqlite3
from collections import Counter
from datetime import datetime, timezone


EXCLUDED_STATUS = "excluded_no_identity_bridge"
EXCLUDED_DECISIONS = ("blocked", "needs_review")


def main() -> None:
    parser = argparse.ArgumentParser(description="Exclude identity records without an explicit device bridge")
    parser.add_argument("--result-root", required=True)
    args = parser.parse_args()

    root = pathlib.Path(args.result_root).resolve()
    db_path = root / "identity_semantics.sqlite3"
    connection = sqlite3.connect(db_path)
    connection.execute("PRAGMA busy_timeout=30000")
    excluded_at = datetime.now(timezone.utc).isoformat()
    connection.execute("BEGIN IMMEDIATE")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS identity_exclusion_audit (
            exclusion_id INTEGER PRIMARY KEY AUTOINCREMENT,
            entity_type TEXT NOT NULL,
            entity_id TEXT NOT NULL,
            source_schema TEXT,
            source_table_group TEXT,
            source_table TEXT,
            source_row_id TEXT,
            original_status TEXT NOT NULL,
            exclusion_status TEXT NOT NULL,
            reason_code TEXT NOT NULL,
            evidence_json TEXT,
            excluded_at TEXT NOT NULL,
            recoverable INTEGER NOT NULL,
            source_delete INTEGER NOT NULL,
            formal_publication INTEGER NOT NULL,
            UNIQUE(entity_type, entity_id)
        )
        """
    )

    candidate_rows = connection.execute(
        """
        SELECT candidate_id, source_schema, source_table_group, source_table,
               source_row_id, decision, evidence_json
          FROM device_match_candidate
         WHERE decision IN (?, ?)
         ORDER BY candidate_id
        """,
        EXCLUDED_DECISIONS,
    ).fetchall()
    audit_rows = [
        (
            "device_match_candidate",
            str(row[0]),
            row[1],
            row[2],
            row[3],
            row[4],
            row[5],
            EXCLUDED_STATUS,
            "INSUFFICIENT_IDENTITY_BRIDGE",
            row[6],
            excluded_at,
            1,
            0,
            0,
        )
        for row in candidate_rows
    ]
    connection.executemany(
        """
        INSERT OR IGNORE INTO identity_exclusion_audit
        (entity_type, entity_id, source_schema, source_table_group, source_table,
         source_row_id, original_status, exclusion_status, reason_code, evidence_json,
         excluded_at, recoverable, source_delete, formal_publication)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        audit_rows,
    )
    connection.execute(
        "UPDATE device_match_candidate SET decision=? WHERE decision IN (?, ?)",
        (EXCLUDED_STATUS, *EXCLUDED_DECISIONS),
    )

    map_rows = connection.execute(
        """
        SELECT map_id, source_schema, source_table_group, source_table,
               source_row_id, status, evidence_json
          FROM device_identity_map
         WHERE status IN (?, ?)
         ORDER BY map_id
        """,
        EXCLUDED_DECISIONS,
    ).fetchall()
    map_audit_rows = [
        (
            "device_identity_map",
            str(row[0]),
            row[1],
            row[2],
            row[3],
            row[4],
            row[5],
            EXCLUDED_STATUS,
            "INSUFFICIENT_IDENTITY_BRIDGE",
            row[6],
            excluded_at,
            1,
            0,
            0,
        )
        for row in map_rows
    ]
    connection.executemany(
        """
        INSERT OR IGNORE INTO identity_exclusion_audit
        (entity_type, entity_id, source_schema, source_table_group, source_table,
         source_row_id, original_status, exclusion_status, reason_code, evidence_json,
         excluded_at, recoverable, source_delete, formal_publication)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        map_audit_rows,
    )
    connection.execute(
        "UPDATE device_identity_map SET status=? WHERE status IN (?, ?)",
        (EXCLUDED_STATUS, *EXCLUDED_DECISIONS),
    )

    event_rows = connection.execute(
        """
        SELECT event_record_id, source_schema, event_type, source_table,
               source_row_id, link_status, evidence_json
          FROM device_event
         WHERE link_status IN (?, ?)
         ORDER BY event_record_id
        """,
        EXCLUDED_DECISIONS,
    ).fetchall()
    event_audit_rows = [
        (
            "device_event",
            str(row[0]),
            row[1],
            row[2],
            row[3],
            row[4],
            row[5],
            EXCLUDED_STATUS,
            "INSUFFICIENT_IDENTITY_BRIDGE",
            row[6],
            excluded_at,
            1,
            0,
            0,
        )
        for row in event_rows
    ]
    connection.executemany(
        """
        INSERT OR IGNORE INTO identity_exclusion_audit
        (entity_type, entity_id, source_schema, source_table_group, source_table,
         source_row_id, original_status, exclusion_status, reason_code, evidence_json,
         excluded_at, recoverable, source_delete, formal_publication)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        event_audit_rows,
    )
    connection.execute(
        "UPDATE device_event SET link_status=? WHERE link_status IN (?, ?)",
        (EXCLUDED_STATUS, *EXCLUDED_DECISIONS),
    )

    # The sample is a processing queue, not an evidence archive.  Keep only
    # accepted candidates in it so no excluded item can be reviewed by mistake.
    connection.execute(
        """
        DELETE FROM identity_sample_200
         WHERE candidate_id IN (
             SELECT candidate_id FROM device_match_candidate
              WHERE decision=?
         )
        """,
        (EXCLUDED_STATUS,),
    )

    connection.commit()
    report = {
        "status": "excluded_without_identity_bridge",
        "excluded_candidate_count": len(candidate_rows),
        "excluded_map_count": len(map_rows),
        "excluded_event_count": len(event_rows),
        "excluded_by_candidate_status": dict(Counter(row[5] for row in candidate_rows)),
        "excluded_by_event_status": dict(Counter(row[5] for row in event_rows)),
        "audit_total": connection.execute("SELECT COUNT(*) FROM identity_exclusion_audit").fetchone()[0],
        "accepted_candidate_count": connection.execute(
            "SELECT COUNT(*) FROM device_match_candidate WHERE decision='accepted'"
        ).fetchone()[0],
        "sample_count_after": connection.execute("SELECT COUNT(*) FROM identity_sample_200").fetchone()[0],
        "source_delete": False,
        "formal_publication": False,
        "recoverable": True,
        "reason_code": "INSUFFICIENT_IDENTITY_BRIDGE",
        "excluded_at": excluded_at,
    }
    connection.close()
    output = root / "identity-exclusion-no-bridge-report.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"report": str(output), **report}, ensure_ascii=False))


if __name__ == "__main__":
    main()
