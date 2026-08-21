"""Run lightweight invariants for the blocked inspection evidence pass."""
from __future__ import annotations

import argparse
import json
import pathlib
import sqlite3


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--result-root", required=True)
    args = parser.parse_args()
    root = pathlib.Path(args.result_root).resolve()
    con = sqlite3.connect(root / "identity_semantics.sqlite3")
    one = lambda sql: con.execute(sql).fetchone()[0]
    has_table = lambda name: con.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()[0] > 0
    has_evidence_table = has_table("inspection_identity_evidence")
    has_bridge_table = has_table("inspection_device_bridge")
    report = {
        "quick_check": one("PRAGMA quick_check"),
        "device_identity_duplicates": one(
            "SELECT COUNT(*) FROM (SELECT master_source_schema, site_id, asset_number FROM unified_device GROUP BY 1,2,3 HAVING COUNT(*)>1)"
        ),
        "inspection_blocked_with_device": one(
            "SELECT COUNT(*) FROM device_event WHERE event_type='inspection' AND link_status='blocked' AND unified_device_id IS NOT NULL"
        ),
        "inspection_nonaccepted_pending": one(
            "SELECT COUNT(*) FROM device_event WHERE event_type='inspection' AND link_status IN ('blocked','needs_review')"
        ),
        "inspection_excluded_no_bridge": one(
            "SELECT COUNT(*) FROM device_event WHERE event_type='inspection' AND link_status='excluded_no_identity_bridge'"
        ),
        "inspection_excluded_with_device": one(
            "SELECT COUNT(*) FROM device_event WHERE event_type='inspection' AND link_status='excluded_no_identity_bridge' AND unified_device_id IS NOT NULL"
        ),
        "inspection_accepted": one(
            "SELECT COUNT(*) FROM device_event WHERE event_type='inspection' AND link_status='accepted'"
        ),
        "inspection_needs_review": one(
            "SELECT COUNT(*) FROM device_event WHERE event_type='inspection' AND link_status='needs_review'"
        ),
        "inspection_evidence_rows": one("SELECT COUNT(*) FROM inspection_identity_evidence") if has_evidence_table else 0,
        "inspection_bridge_rows": one("SELECT COUNT(*) FROM inspection_device_bridge") if has_bridge_table else 0,
        "inspection_bridge_pending": one(
            "SELECT COUNT(*) FROM inspection_device_bridge WHERE mapping_status='pending' AND validation_status='not_validated'"
        ) if has_bridge_table else 0,
        "evidence_status": [list(row) for row in con.execute(
            "SELECT evidence_status, COUNT(*) FROM inspection_identity_evidence GROUP BY 1 ORDER BY 1"
        )] if has_evidence_table else [],
        "inspection_candidate_decisions": [list(row) for row in con.execute(
            "SELECT decision, COUNT(*) FROM device_match_candidate WHERE source_table_group='inspection' GROUP BY 1 ORDER BY 1"
        )],
    }
    con.close()
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
