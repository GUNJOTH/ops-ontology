"""Read-only verification report for a built identity result layer."""
from __future__ import annotations

import json
import pathlib
import sqlite3
import sys


def main() -> None:
    root = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else sorted(
        (pathlib.Path(__file__).resolve().parent / "results").glob("identity-layer-v1-*/"), reverse=True
    )[0]
    db = root / "identity_semantics.sqlite3"
    connection = sqlite3.connect(db)
    checks: dict[str, object] = {
        "integrity": connection.execute("PRAGMA integrity_check").fetchone()[0],
        "sample_schema_group": [list(row) for row in connection.execute(
            "SELECT c.source_schema, c.source_table_group, COUNT(*) "
            "FROM identity_sample_200 s JOIN device_match_candidate c ON c.candidate_id=s.candidate_id "
            "GROUP BY 1,2 ORDER BY 1,2"
        )],
        "sample_sites": [list(row) for row in connection.execute(
            "SELECT c.source_schema, c.site_id, COUNT(*) "
            "FROM identity_sample_200 s JOIN device_match_candidate c ON c.candidate_id=s.candidate_id "
            "GROUP BY 1,2 ORDER BY 1,2"
        )],
        "sample_decisions": [list(row) for row in connection.execute(
            "SELECT c.decision, COUNT(*) FROM identity_sample_200 s "
            "JOIN device_match_candidate c ON c.candidate_id=s.candidate_id GROUP BY 1 ORDER BY 1"
        )],
        "accepted_examples": [list(row) for row in connection.execute(
            "SELECT source_schema, source_table_group, source_table, site_id, source_key_type, source_key, "
            "candidate_asset_number, match_method, score FROM device_match_candidate "
            "WHERE decision='accepted' ORDER BY candidate_id LIMIT 10"
        )],
        "review_examples": [list(row) for row in connection.execute(
            "SELECT source_schema, source_table_group, source_table, site_id, raw_description, location_code, "
            "candidate_asset_number, match_method, score FROM device_match_candidate "
            "WHERE decision='needs_review' ORDER BY candidate_id LIMIT 10"
        )],
        "orphan_sample_count": connection.execute(
            "SELECT COUNT(*) FROM identity_sample_200 s LEFT JOIN device_match_candidate c "
            "ON c.candidate_id=s.candidate_id WHERE c.candidate_id IS NULL"
        ).fetchone()[0],
    }
    connection.close()
    print(json.dumps(checks, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
