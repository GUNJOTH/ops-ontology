"""Verify the local relational semantic context layer."""
from __future__ import annotations

import json
import pathlib
import sqlite3
import sys


def main() -> None:
    root = pathlib.Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else sorted(
        (pathlib.Path(__file__).resolve().parent / "results").glob("identity-layer-v1-*/"), reverse=True
    )[0]
    connection = sqlite3.connect(root / "identity_semantics.sqlite3")
    report = {
        "quick_check": connection.execute("PRAGMA quick_check").fetchone()[0],
        "device_count": connection.execute("SELECT COUNT(1) FROM unified_device").fetchone()[0],
        "function_location_count": connection.execute("SELECT COUNT(1) FROM function_location").fetchone()[0],
        "context_link_count": connection.execute("SELECT COUNT(1) FROM device_context_link").fetchone()[0],
        "event_counts": [list(row) for row in connection.execute(
            "SELECT event_type, link_status, COUNT(1) FROM device_event GROUP BY 1,2 ORDER BY 1,2"
        )],
        "latest_inspection_count": connection.execute("SELECT COUNT(1) FROM v_latest_inspection").fetchone()[0],
        "latest_inspection_candidate_count": connection.execute("SELECT COUNT(1) FROM v_latest_inspection_candidate").fetchone()[0],
        "inspection_review_audit_count": connection.execute("SELECT COUNT(1) FROM inspection_identity_review").fetchone()[0],
        "duplicate_context_links": connection.execute(
            "SELECT COUNT(1) FROM (SELECT unified_device_id, context_type, context_record_id FROM device_context_link GROUP BY 1,2,3 HAVING COUNT(1)>1)"
        ).fetchone()[0],
    }
    connection.close()
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
