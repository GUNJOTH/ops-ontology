"""Read-only release-boundary verification for the local dual-database system."""
from __future__ import annotations

import json
import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
SQLITE_DB = DATA_DIR / "semantic_workflow.sqlite3"
DUCKDB_DB = DATA_DIR / "semantic_analytics_v155.duckdb"


def load_duckdb():
    """Load a DuckDB wheel compiled for the active CPython ABI.

    The project keeps Windows DuckDB wheels under versioned dependency
    directories. Importing a cp312 extension from Python 3.11 can crash the
    interpreter before Python can report a normal exception, so select the
    matching directory before importing DuckDB.
    """
    abi = f"cp{sys.version_info.major}{sys.version_info.minor}"
    candidates = [ROOT / ".deps_latest", ROOT / ".deps_121", ROOT / ".deps"]
    for dependency_dir in candidates:
        duckdb_dir = dependency_dir / "duckdb"
        native_extensions = list(duckdb_dir.glob(f"*{abi}-win_amd64.pyd")) if duckdb_dir.exists() else []
        native_extensions.extend(dependency_dir.glob(f"_*{abi}-win_amd64.pyd"))
        if not native_extensions:
            continue
        sys.path.insert(0, str(dependency_dir))
        try:
            import duckdb  # type: ignore
        except ImportError as exc:
            raise SystemExit(
                f"DuckDB dependency exists but could not be loaded for Python {abi}: {exc}"
            ) from exc
        return duckdb, dependency_dir
    raise SystemExit(
        f"No DuckDB dependency compiled for Python {abi}. "
        "Use the matching bundled Python runtime or install a matching wheel."
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--duckdb",
        type=Path,
        default=DUCKDB_DB,
        help="要校验的 DuckDB 文件，默认使用正式分析库",
    )
    args = parser.parse_args()
    duckdb, dependency_dir = load_duckdb()

    sqlite_connection = sqlite3.connect(f"file:{SQLITE_DB}?mode=ro", uri=True)
    sqlite_connection.row_factory = sqlite3.Row
    batch = sqlite_connection.execute("SELECT * FROM batch_run ORDER BY started_at DESC LIMIT 1").fetchone()
    if not batch:
        raise SystemExit("SQLite 中没有批次")
    snapshot = sqlite_connection.execute(
        "SELECT * FROM source_snapshot WHERE source_snapshot_id=?",
        (batch["source_snapshot_id"],),
    ).fetchone()
    if not snapshot:
        raise SystemExit("SQLite 当前批次引用的 source_snapshot 不存在")
    sqlite_metrics = {
        "batch_id": batch["batch_id"],
        "run_id": batch["run_id"],
        "source_snapshot_id": batch["source_snapshot_id"],
        "source_snapshot_hash": snapshot["snapshot_hash"],
        "input_count": batch["input_count"],
        "device_rows": sqlite_connection.execute("SELECT count(*) FROM device_identity WHERE source_snapshot_id=?", (batch["source_snapshot_id"],)).fetchone()[0],
        "candidate_rows": sqlite_connection.execute("SELECT count(*) FROM semantic_candidate WHERE batch_id=?", (batch["batch_id"],)).fetchone()[0],
        "distinct_candidate_ids": sqlite_connection.execute("SELECT count(DISTINCT candidate_id) FROM semantic_candidate WHERE batch_id=?", (batch["batch_id"],)).fetchone()[0],
        "distinct_identities": sqlite_connection.execute("SELECT count(*) FROM (SELECT DISTINCT source_schema,site_id,asset_number FROM device_identity WHERE source_snapshot_id=?)", (batch["source_snapshot_id"],)).fetchone()[0],
        "published_rows": sqlite_connection.execute("SELECT count(*) FROM published_description").fetchone()[0],
        "batch_published_count": batch["published_count"],
        "formal_publication": bool(batch["formal_publication"]),
        "validation_rows": sqlite_connection.execute(
            """
            SELECT count(*)
            FROM validation_result v
            JOIN semantic_candidate c ON c.candidate_id=v.candidate_id
            WHERE c.batch_id=?
            """,
            (batch["batch_id"],),
        ).fetchone()[0],
        "validation_candidates": sqlite_connection.execute(
            """
            SELECT count(DISTINCT v.candidate_id)
            FROM validation_result v
            JOIN semantic_candidate c ON c.candidate_id=v.candidate_id
            WHERE c.batch_id=?
            """,
            (batch["batch_id"],),
        ).fetchone()[0],
        "validation_types": sqlite_connection.execute(
            """
            SELECT count(DISTINCT v.validator_type)
            FROM validation_result v
            JOIN semantic_candidate c ON c.candidate_id=v.candidate_id
            WHERE c.batch_id=?
            """,
            (batch["batch_id"],),
        ).fetchone()[0],
        "validation_failures": sqlite_connection.execute(
            """
            SELECT count(*)
            FROM validation_result v
            JOIN semantic_candidate c ON c.candidate_id=v.candidate_id
            WHERE c.batch_id=? AND v.outcome='fail'
            """,
            (batch["batch_id"],),
        ).fetchone()[0],
        "enabled_unapproved_rules": sqlite_connection.execute(
            """
            SELECT count(*)
            FROM cleaning_rule_registry r
            JOIN cleaning_run u ON u.rule_key=r.rule_key AND u.replay_id=r.replay_id
            WHERE r.enabled=1
              AND u.status NOT IN ('approved','published')
              AND NOT EXISTS (
                SELECT 1
                FROM formal_approval_queue q
                WHERE q.replay_id=r.replay_id
                  AND q.status='pending'
              )
            """
        ).fetchone()[0],
        "latest_replay_status": (sqlite_connection.execute(
            """
            SELECT rr.status
            FROM replay_run rr
            JOIN cleaning_rule_registry r ON r.replay_id=rr.replay_id
            JOIN cleaning_run u ON u.rule_key=r.rule_key AND u.replay_id=r.replay_id
            WHERE u.status IN ('approved','published')
            ORDER BY rr.started_at DESC
            LIMIT 1
            """
        ).fetchone() or {"status": "not_applicable"})["status"],
        "latest_replay_failures": (sqlite_connection.execute(
            """
            SELECT rr.fail_count
            FROM replay_run rr
            JOIN cleaning_rule_registry r ON r.replay_id=rr.replay_id
            JOIN cleaning_run u ON u.rule_key=r.rule_key AND u.replay_id=r.replay_id
            WHERE u.status IN ('approved','published')
            ORDER BY rr.started_at DESC
            LIMIT 1
            """
        ).fetchone() or {"fail_count": 0})["fail_count"],
        "unapproved_publications": sqlite_connection.execute(
            "SELECT count(*) FROM published_description p LEFT JOIN review_decision r ON r.review_id=p.review_id WHERE r.review_id IS NULL OR r.decision NOT IN ('approved','modified')"
        ).fetchone()[0],
        "foreign_key_failures": len(sqlite_connection.execute("PRAGMA foreign_key_check").fetchall()),
    }
    sqlite_connection.close()

    duck_connection = duckdb.connect(str(args.duckdb), read_only=True)
    duck_metadata = {
        row[0]: row[1]
        for row in duck_connection.execute(
            "SELECT metadata_key, metadata_value FROM analytics_metadata"
        ).fetchall()
    }
    duck_metrics = {
        "fact_rows": duck_connection.execute("SELECT count(*) FROM semantic_candidate_fact").fetchone()[0],
        "distinct_candidate_ids": duck_connection.execute("SELECT count(DISTINCT CANDIDATE_ID) FROM semantic_candidate_fact").fetchone()[0],
        "distinct_identities": duck_connection.execute("SELECT count(*) FROM (SELECT DISTINCT SITEID,ASSETNUM FROM semantic_candidate_fact)").fetchone()[0],
        "invalid_context_json": duck_connection.execute("SELECT count(*) FROM semantic_candidate_fact WHERE try_cast(CONTEXT_JSON AS JSON) IS NULL").fetchone()[0],
        "site_count": duck_connection.execute("SELECT count(DISTINCT SITEID) FROM semantic_candidate_fact").fetchone()[0],
        "baseline_id": duck_metadata.get("baseline_id"),
        "batch_id": duck_metadata.get("batch_id"),
        "run_id": duck_metadata.get("run_id"),
        "source_snapshot_id": duck_metadata.get("source_snapshot_id"),
        "source_snapshot_hash": duck_metadata.get("source_snapshot_hash"),
        "input_count": duck_metadata.get("input_count"),
    }
    duck_connection.close()

    expected = sqlite_metrics["input_count"]
    failures = []
    if duck_metrics["batch_id"] != sqlite_metrics["batch_id"]:
        failures.append(
            f"BASELINE_BATCH_MISMATCH:{duck_metrics['batch_id']}!={sqlite_metrics['batch_id']}"
        )
    if duck_metrics["source_snapshot_id"] != sqlite_metrics["source_snapshot_id"]:
        failures.append(
            "BASELINE_SOURCE_SNAPSHOT_MISMATCH:"
            f"{duck_metrics['source_snapshot_id']}!={sqlite_metrics['source_snapshot_id']}"
        )
    if duck_metrics["source_snapshot_hash"] != sqlite_metrics["source_snapshot_hash"]:
        failures.append("BASELINE_SOURCE_SNAPSHOT_HASH_MISMATCH")
    if duck_metrics["input_count"] != str(expected):
        failures.append(f"BASELINE_INPUT_COUNT_METADATA:{duck_metrics['input_count']}!={expected}")
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
    if sqlite_metrics["validation_rows"] != sqlite_metrics["candidate_rows"] * 5:
        failures.append("VALIDATION_DETAIL_INCOMPLETE")
    if sqlite_metrics["validation_candidates"] != sqlite_metrics["candidate_rows"]:
        failures.append("VALIDATION_CANDIDATE_COVERAGE_INCOMPLETE")
    if sqlite_metrics["validation_types"] != 5:
        failures.append("VALIDATION_TYPE_COVERAGE_INCOMPLETE")
    if sqlite_metrics["validation_failures"]:
        failures.append("VALIDATION_FAILURE")
    if sqlite_metrics["enabled_unapproved_rules"]:
        failures.append("UNAPPROVED_RULE_ENABLED")
    if sqlite_metrics["latest_replay_status"] != "passed" or sqlite_metrics["latest_replay_failures"]:
        failures.append("LATEST_REPLAY_NOT_PASSED")

    result = {
        "status": "PASS" if not failures else "FAIL",
        "sqlite": sqlite_metrics,
        "duckdb": duck_metrics,
        "failures": failures,
        "source_write": False,
        "formal_publication": sqlite_metrics["formal_publication"],
        "duckdb_runtime": str(dependency_dir),
        "duckdb_database": str(args.duckdb),
        "python_abi": f"cp{sys.version_info.major}{sys.version_info.minor}",
    }
    (DATA_DIR / "verification.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
