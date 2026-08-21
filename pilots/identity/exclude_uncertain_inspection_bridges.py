"""Exclude uncertain inspection-device bridge rows from the local work queue.

This is a recoverable local status change. DM8 source rows, device events and
accepted identity links are not deleted or rewritten.
"""
from __future__ import annotations

import argparse
import csv
import json
import pathlib
import sqlite3
from datetime import datetime, timezone


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--result-root", required=True)
    parser.add_argument("--reason", default="evidence_uncertain_excluded_from_mapping_queue")
    args = parser.parse_args()
    result_root = pathlib.Path(args.result_root).resolve()
    db_path = result_root / "identity_semantics.sqlite3"
    now = datetime.now(timezone.utc).isoformat()
    con = sqlite3.connect(db_path)
    con.execute(
        """CREATE TABLE IF NOT EXISTS inspection_bridge_exclusion_audit (
             exclusion_id TEXT PRIMARY KEY,
             event_record_id TEXT NOT NULL,
             previous_mapping_status TEXT NOT NULL,
             previous_validation_status TEXT NOT NULL,
             reason TEXT NOT NULL,
             created_at TEXT NOT NULL
           )"""
    )
    rows = con.execute(
        """SELECT bridge_id, event_record_id, mapping_status, validation_status
             FROM inspection_device_bridge
            WHERE mapping_status='pending' AND validation_status='not_validated'
            ORDER BY event_record_id"""
    ).fetchall()
    for bridge_id, event_record_id, mapping_status, validation_status in rows:
        con.execute(
            """INSERT OR IGNORE INTO inspection_bridge_exclusion_audit
               VALUES (?,?,?,?,?,?)""",
            (f"EX-{bridge_id}", event_record_id, mapping_status, validation_status, args.reason, now),
        )
    con.execute(
        """UPDATE inspection_device_bridge
              SET mapping_status='excluded', validation_status='excluded_uncertain',
                  review_reason=?, updated_at=?
            WHERE mapping_status='pending' AND validation_status='not_validated'""",
        (args.reason, now),
    )
    con.execute(
        """UPDATE inspection_identity_evidence
              SET evidence_status='blocked', recommended_action='excluded_uncertain',
                  reason_codes_json=?
            WHERE event_record_id IN (SELECT event_record_id FROM inspection_bridge_exclusion_audit)""",
        (json.dumps(["excluded_uncertain", args.reason], ensure_ascii=False),),
    )
    con.commit()
    status_counts = con.execute(
        "SELECT mapping_status, validation_status, COUNT(*) FROM inspection_device_bridge GROUP BY 1,2 ORDER BY 1,2"
    ).fetchall()
    audit_count = con.execute("SELECT COUNT(*) FROM inspection_bridge_exclusion_audit").fetchone()[0]
    con.close()

    template = result_root / "inspection-device-bridge-template.csv"
    if template.exists():
        with template.open("r", encoding="utf-8-sig", newline="") as handle:
            template_rows = list(csv.DictReader(handle))
            fieldnames = list(template_rows[0].keys()) if template_rows else []
        for row in template_rows:
            row["mapping_status"] = "excluded"
            row["validation_status"] = "excluded_uncertain"
            row["review_reason"] = args.reason
        with template.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(template_rows)

    report = {
        "excluded_count": len(rows),
        "audit_count": audit_count,
        "status_counts": [list(row) for row in status_counts],
        "reason": args.reason,
        "source_write": False,
        "formal_publication": False,
        "recoverable": True,
    }
    report_path = result_root / "inspection-bridge-exclusion-report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({**report, "report": str(report_path)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
