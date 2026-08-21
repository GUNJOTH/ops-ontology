"""Persist blocked-inspection evidence without changing identity decisions."""
from __future__ import annotations

import argparse
import json
import pathlib
import sqlite3
from datetime import datetime, timezone

from safe_convert import to_int


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--result-root", required=True)
    parser.add_argument("--analysis", required=True)
    args = parser.parse_args()
    result_root = pathlib.Path(args.result_root).resolve()
    analysis_path = pathlib.Path(args.analysis).resolve()
    payload = json.loads(analysis_path.read_text(encoding="utf-8"))
    connection = sqlite3.connect(result_root / "identity_semantics.sqlite3")
    connection.execute(
        """CREATE TABLE IF NOT EXISTS inspection_identity_evidence (
             evidence_id TEXT PRIMARY KEY,
             event_record_id TEXT NOT NULL UNIQUE,
             source_schema TEXT NOT NULL,
             source_table TEXT NOT NULL,
             source_row_id TEXT NOT NULL,
             site_id TEXT,
             location_code TEXT,
             evidence_status TEXT NOT NULL,
             recommended_action TEXT NOT NULL,
             reason_codes_json TEXT NOT NULL,
             evidence_json TEXT NOT NULL,
             source_snapshot_id TEXT NOT NULL,
             created_at TEXT NOT NULL,
             updated_at TEXT
           )"""
    )
    existing_columns = {row[1] for row in connection.execute("PRAGMA table_info(inspection_identity_evidence)")}
    if "updated_at" not in existing_columns:
        connection.execute("ALTER TABLE inspection_identity_evidence ADD COLUMN updated_at TEXT")
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_inspection_identity_evidence_action ON inspection_identity_evidence(recommended_action)"
    )
    created_at = datetime.now(timezone.utc).isoformat()
    analysis_id = analysis_path.stem
    persisted = 0
    for row in payload.get("rows", []):
        action = str(row.get("recommended_action") or "blocked_no_device_bridge")
        status = "needs_review" if action.startswith("needs_review") else "blocked"
        evidence = row.get("evidence") or {}
        compact_evidence = {
            "analysis_id": analysis_id,
            "source_row_aligned": bool(evidence.get("old_fresh_row_aligned")),
            "business_record_fields": evidence.get("business_record_fields", {}),
            "direct_asset_number": evidence.get("direct_asset_number", ""),
            "direct_asset_candidate_count": len(evidence.get("direct_asset_candidates", [])),
            "itemnum": evidence.get("itemnum", ""),
            "itemnum_asset_candidate_count": len(evidence.get("itemnum_asset_candidates", [])),
            "location_asset_candidate_count": len(evidence.get("location_asset_candidates", [])),
            "location_kks_format_signal": bool(evidence.get("location_kks_format_signal")),
            "raw_location_count": len(evidence.get("raw_location_rows", [])),
            "raw_parent_locations": evidence.get("raw_parent_locations", []),
            "raw_parent_row_count": len(evidence.get("raw_parent_rows", [])),
            "prefix_asset_count": to_int(evidence.get("prefix_asset_count", 0)),
            "source_write": False,
            "formal_publication": False,
        }
        reason_codes = [action]
        evidence_id = f"IE-{row['event_record_id']}"
        reason_codes_json = json.dumps(reason_codes, ensure_ascii=False)
        evidence_json = json.dumps(compact_evidence, ensure_ascii=False, sort_keys=True)
        connection.execute(
            """INSERT INTO inspection_identity_evidence
               (evidence_id,event_record_id,source_schema,source_table,source_row_id,site_id,location_code,
                evidence_status,recommended_action,reason_codes_json,evidence_json,source_snapshot_id,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(evidence_id) DO UPDATE SET
                event_record_id=excluded.event_record_id,
                source_schema=excluded.source_schema,
                source_table=excluded.source_table,
                source_row_id=excluded.source_row_id,
                site_id=excluded.site_id,
                location_code=excluded.location_code,
                evidence_status=excluded.evidence_status,
                recommended_action=excluded.recommended_action,
                reason_codes_json=excluded.reason_codes_json,
                evidence_json=excluded.evidence_json,
                source_snapshot_id=excluded.source_snapshot_id,
                updated_at=excluded.updated_at
               WHERE inspection_identity_evidence.evidence_json != excluded.evidence_json""",
            (
                evidence_id,
                row["event_record_id"],
                row.get("source_schema", ""),
                row.get("source_table", ""),
                row.get("source_row_id", ""),
                row.get("site_id", ""),
                row.get("location_code", ""),
                status,
                action,
                reason_codes_json,
                evidence_json,
                analysis_id,
                created_at,
                created_at,
            ),
        )
        for table_name, key_column in (("device_match_candidate", "candidate_id"), ("device_event", "event_record_id")):
            if table_name == "device_match_candidate":
                existing = connection.execute(
                    """SELECT candidate_id, evidence_json FROM device_match_candidate
                        WHERE source_schema=? AND source_table_group='inspection'
                          AND source_table=? AND source_row_id=?""",
                    (row.get("source_schema", ""), row.get("source_table", ""), row.get("source_row_id", "")),
                ).fetchone()
            else:
                existing = connection.execute(
                    "SELECT event_record_id, evidence_json FROM device_event WHERE event_record_id=?",
                    (row["event_record_id"],),
                ).fetchone()
            if not existing:
                continue
            try:
                old_evidence = json.loads(existing[1] or "{}")
            except json.JSONDecodeError:
                old_evidence = {"raw": existing[1] or ""}
            old_evidence["bridge_analysis"] = compact_evidence
            old_evidence["bridge_reason_codes"] = reason_codes
            if table_name == "device_match_candidate":
                connection.execute(
                    "UPDATE device_match_candidate SET evidence_json=? WHERE candidate_id=?",
                    (json.dumps(old_evidence, ensure_ascii=False, sort_keys=True), existing[0]),
                )
            else:
                connection.execute(
                    "UPDATE device_event SET evidence_json=? WHERE event_record_id=?",
                    (json.dumps(old_evidence, ensure_ascii=False, sort_keys=True), existing[0]),
                )
        persisted += 1
    connection.commit()
    status_counts = connection.execute(
        "SELECT evidence_status, COUNT(*) FROM inspection_identity_evidence GROUP BY evidence_status ORDER BY evidence_status"
    ).fetchall()
    decision_counts = connection.execute(
        "SELECT link_status, COUNT(*) FROM device_event WHERE event_type='inspection' GROUP BY link_status ORDER BY link_status"
    ).fetchall()
    connection.close()
    print(json.dumps({
        "persisted": persisted,
        "evidence_status_counts": dict(status_counts),
        "inspection_link_status_counts": dict(decision_counts),
        "source_write": False,
        "formal_publication": False,
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
