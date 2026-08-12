"""Read-only release-boundary verification for the local dual-database system."""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
SQLITE_DB = DATA_DIR / "semantic_workflow.sqlite3"
DUCKDB_DB = DATA_DIR / "semantic_analytics_v155.duckdb"
DEPENDENCY_DIR = ROOT / ".deps_latest"


def main() -> None:
    sys.path.insert(0, str(DEPENDENCY_DIR))
    import duckdb  # type: ignore

    sqlite_connection = sqlite3.connect(f"file:{SQLITE_DB}?mode=ro", uri=True)
    sqlite_connection.row_factory = sqlite3.Row
    batch = sqlite_connection.execute("SELECT * FROM batch_run ORDER BY started_at DESC LIMIT 1").fetchone()
    if not batch:
        raise SystemExit("SQLite 中没有批次")
    sqlite_metrics = {
        "batch_id": batch["batch_id"],
        "input_count": batch["input_count"],
        "device_rows": sqlite_connection.execute("SELECT count(*) FROM device_identity WHERE source_snapshot_id=?", (batch["source_snapshot_id"],)).fetchone()[0],
        "candidate_rows": sqlite_connection.execute("SELECT count(*) FROM semantic_candidate WHERE batch_id=?", (batch["batch_id"],)).fetchone()[0],
        "distinct_candidate_ids": sqlite_connection.execute("SELECT count(DISTINCT candidate_id) FROM semantic_candidate WHERE batch_id=?", (batch["batch_id"],)).fetchone()[0],
        "distinct_identities": sqlite_connection.execute("SELECT count(*) FROM (SELECT DISTINCT source_schema,site_id,asset_number FROM device_identity WHERE source_snapshot_id=?)", (batch["source_snapshot_id"],)).fetchone()[0],
        "published_rows": sqlite_connection.execute("SELECT count(*) FROM published_description").fetchone()[0],
        "unapproved_publications": sqlite_connection.execute(
            "SELECT count(*) FROM published_description p LEFT JOIN review_decision r ON r.review_id=p.review_id WHERE r.review_id IS NULL OR r.decision NOT IN ('approved','modified')"
        ).fetchone()[0],
        "foreign_key_failures": len(sqlite_connection.execute("PRAGMA foreign_key_check").fetchall()),
    }
    sqlite_connection.close()

    duck_connection = duckdb.connect(str(DUCKDB_DB), read_only=True)
    duck_metrics = {
        "fact_rows": duck_connection.execute("SELECT count(*) FROM semantic_candidate_fact").fetchone()[0],
        "distinct_candidate_ids": duck_connection.execute("SELECT count(DISTINCT CANDIDATE_ID) FROM semantic_candidate_fact").fetchone()[0],
        "distinct_identities": duck_connection.execute("SELECT count(*) FROM (SELECT DISTINCT SITEID,ASSETNUM FROM semantic_candidate_fact)").fetchone()[0],
        "invalid_context_json": duck_connection.execute("SELECT count(*) FROM semantic_candidate_fact WHERE try_cast(CONTEXT_JSON AS JSON) IS NULL").fetchone()[0],
        "site_count": duck_connection.execute("SELECT count(DISTINCT SITEID) FROM semantic_candidate_fact").fetchone()[0],
    }
    duck_connection.close()

    expected = sqlite_metrics["input_count"]
    failures = []
    for name, value in {
        "sqlite_device_rows": sqlite_metrics["device_rows"],
        "sqlite_candidate_rows": sqlite_metrics["candidate_rows"],
        "sqlite_distinct_candidate_ids": sqlite_metrics["distinct_candidate_ids"],
        "sqlite_distinct_identities": sqlite_metrics["distinct_identities"],
        "duckdb_fact_rows": duck_metrics["fact_rows"],
        "duckdb_distinct_candidate_ids": duck_metrics["distinct_candidate_ids"],
        "duckdb_distinct_identities": duck_metrics["distinct_identities"],
    }.items():
        if value != expected:
            failures.append(f"{name}:{value}!={expected}")
    if sqlite_metrics["unapproved_publications"]:
        failures.append("UNAPPROVED_PUBLICATION")
    if sqlite_metrics["foreign_key_failures"]:
        failures.append("SQLITE_FOREIGN_KEY_FAILURE")
    if duck_metrics["invalid_context_json"]:
        failures.append("INVALID_CONTEXT_JSON")

    result = {
        "status": "PASS" if not failures else "FAIL",
        "sqlite": sqlite_metrics,
        "duckdb": duck_metrics,
        "failures": failures,
        "source_write": False,
        "formal_publication": False,
    }
    (DATA_DIR / "verification.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
