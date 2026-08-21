"""Report local unified-device and operational-link counts."""
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
    report = {
        "unified_device_total": con.execute("SELECT COUNT(*) FROM unified_device").fetchone()[0],
        "identity_map_by_status": [list(row) for row in con.execute(
            "SELECT status, COUNT(*) FROM device_identity_map GROUP BY 1 ORDER BY 1"
        )],
        "event_by_type_status": [list(row) for row in con.execute(
            "SELECT event_type, link_status, COUNT(*) FROM device_event GROUP BY 1,2 ORDER BY 1,2"
        )],
        "operational_accepted_total": con.execute(
            "SELECT COUNT(*) FROM device_event WHERE link_status='accepted'"
        ).fetchone()[0],
        "identity_map_accepted_total": con.execute(
            "SELECT COUNT(*) FROM device_identity_map WHERE status='accepted'"
        ).fetchone()[0],
        "cross_system_candidates": [list(row) for row in con.execute(
            "SELECT decision, COUNT(*) FROM cross_system_match_candidate GROUP BY 1 ORDER BY 1"
        )],
    }
    con.close()
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
