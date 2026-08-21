"""Build the DuckDB analytics snapshot from the verified SQLite workflow batch.

This is the canonical bridge for the local dual-database baseline. The source
DM/MaxiEAM systems remain read-only; this script only reads SQLite and writes a
new local DuckDB file. It records the source snapshot identity in DuckDB
metadata so verification cannot compare unrelated snapshots by row count.
"""
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
SQLITE_DB = DATA_DIR / "semantic_workflow.sqlite3"
DEFAULT_OUTPUT = DATA_DIR / "semantic_analytics_v155.rebuilt.duckdb"
DEFAULT_MANIFEST = DATA_DIR / "duckdb_baseline_manifest.json"
DEPENDENCY_DIRS = (ROOT / ".deps_latest", ROOT / ".deps_121", ROOT / ".deps")

FACT_COLUMNS = (
    "CANDIDATE_ID", "SOURCE_SNAPSHOT_ID", "SOURCE_ROW_HASH", "ASSETID",
    "SITEID", "ASSETNUM", "ORIGINAL_DESCRIPTION", "CANDIDATE_DESCRIPTION",
    "RESULT_STATUS", "RULE_VERSION", "VALIDATOR_VERSION", "CONTEXT_STATUS",
    "CONTEXT_REASON_CODES", "LOCATION_DESCRIPTION", "LOCATION_STATUS",
    "LOCATION_PARENT", "CLASSSTRUCTURE_DESCRIPTION", "CLASSIFICATION_DESCRIPTION",
    "SPEC_COUNT", "FEATURE_COUNT", "FEATURE_SPEC_COUNT", "METER_COUNT",
    "PARENT_ASSET_COUNT", "RELATION_COUNT", "CONTEXT_JSON", "CONTEXT_HASH",
    "UNIFIED_DESCRIPTION", "SEMANTIC_ACTION", "SEMANTIC_CONFIDENCE",
    "SEMANTIC_RESULT_STATUS", "SEMANTIC_REASON_CODES", "CONTEXT_EVIDENCE_LEVEL",
    "APPLIED_TERM_RULE_IDS", "SEMANTIC_RULE_VERSION", "SEMANTIC_VALIDATOR_VERSION",
    "SEMANTIC_RUN_ID", "SEMANTIC_CANDIDATE_HASH", "SEMANTIC_EVIDENCE_JSON",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_duckdb():
    abi = f"cp{sys.version_info.major}{sys.version_info.minor}"
    for dependency_dir in DEPENDENCY_DIRS:
        duckdb_dir = dependency_dir / "duckdb"
        extension_candidates = list(duckdb_dir.glob(f"*{abi}-win_amd64.pyd")) if duckdb_dir.exists() else []
        extension_candidates.extend(dependency_dir.glob(f"_*{abi}-win_amd64.pyd"))
        if not extension_candidates:
            continue
        sys.path.insert(0, str(dependency_dir))
        import duckdb  # type: ignore

        return duckdb, dependency_dir
    raise SystemExit(f"没有找到适用于 Python {abi} 的 DuckDB 运行时")


def sqlite_readonly() -> sqlite3.Connection:
    if not SQLITE_DB.exists():
        raise SystemExit(f"SQLite 不存在: {SQLITE_DB}")
    connection = sqlite3.connect(f"file:{SQLITE_DB}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def select_batch(connection: sqlite3.Connection, requested_batch: str | None) -> sqlite3.Row:
    if requested_batch:
        row = connection.execute(
            "SELECT * FROM batch_run WHERE batch_id=?", (requested_batch,)
        ).fetchone()
    else:
        row = connection.execute(
            "SELECT * FROM batch_run ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
    if not row:
        raise SystemExit("SQLite 中没有可用批次")
    if row["source_write"] or row["formal_publication"]:
        raise SystemExit("选定批次不满足只读/非正式发布边界，停止重建")
    return row


def build_context(row: sqlite3.Row) -> str:
    context = {
        "identity": {
            "source_schema": row["source_schema"],
            "source_asset_id": row["source_asset_id"] or "",
            "site_id": row["site_id"],
            "asset_number": row["asset_number"],
        },
        "location": {
            "LOCATION": row["location_code"] or "",
            "DESCRIPTION": row["location_description"] or "",
            "PARENT": row["location_parent"] or "",
        },
        "classification": {
            "DESCRIPTION": row["classification_description"] or "",
            "CLASSSTRUCTURE_DESCRIPTION": row["class_structure_description"] or "",
        },
        "context_hash": row["context_hash"],
    }
    return json.dumps(context, ensure_ascii=False, separators=(",", ":"))


def fact_values(row: sqlite3.Row, batch: sqlite3.Row) -> tuple[str, ...]:
    reason_codes = row["reason_codes_json"] or "[]"
    applied_rules = row["applied_rule_ids_json"] or "[]"
    evidence = json.dumps(
        {
            "source_snapshot_id": batch["source_snapshot_id"],
            "source_row_hash": row["source_row_hash"],
            "evidence_level": row["evidence_level"],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    values = {
        "CANDIDATE_ID": row["candidate_id"],
        "SOURCE_SNAPSHOT_ID": batch["source_snapshot_id"],
        "SOURCE_ROW_HASH": row["source_row_hash"],
        "ASSETID": row["source_asset_id"] or "",
        "SITEID": row["site_id"],
        "ASSETNUM": row["asset_number"],
        "ORIGINAL_DESCRIPTION": row["original_description"],
        "CANDIDATE_DESCRIPTION": row["candidate_description"],
        "RESULT_STATUS": row["validator_status"],
        "RULE_VERSION": row["rule_version"],
        "VALIDATOR_VERSION": row["validator_version"],
        "CONTEXT_STATUS": "source_snapshot_joined",
        "CONTEXT_REASON_CODES": reason_codes,
        "LOCATION_DESCRIPTION": row["location_description"] or "",
        "LOCATION_STATUS": "",
        "LOCATION_PARENT": row["location_parent"] or "",
        "CLASSSTRUCTURE_DESCRIPTION": row["class_structure_description"] or "",
        "CLASSIFICATION_DESCRIPTION": row["classification_description"] or "",
        "SPEC_COUNT": "",
        "FEATURE_COUNT": "",
        "FEATURE_SPEC_COUNT": "",
        "METER_COUNT": "",
        "PARENT_ASSET_COUNT": "",
        "RELATION_COUNT": "",
        "CONTEXT_JSON": build_context(row),
        "CONTEXT_HASH": row["context_hash"],
        "UNIFIED_DESCRIPTION": row["candidate_description"],
        "SEMANTIC_ACTION": row["semantic_action"],
        "SEMANTIC_CONFIDENCE": row["confidence"],
        "SEMANTIC_RESULT_STATUS": row["validator_status"],
        "SEMANTIC_REASON_CODES": reason_codes,
        "CONTEXT_EVIDENCE_LEVEL": row["evidence_level"],
        "APPLIED_TERM_RULE_IDS": applied_rules,
        "SEMANTIC_RULE_VERSION": row["rule_version"],
        "SEMANTIC_VALIDATOR_VERSION": row["validator_version"],
        "SEMANTIC_RUN_ID": batch["run_id"],
        "SEMANTIC_CANDIDATE_HASH": row["candidate_hash"],
        "SEMANTIC_EVIDENCE_JSON": evidence,
    }
    return tuple(values[column] for column in FACT_COLUMNS)


def build(output: Path, manifest_path: Path, requested_batch: str | None) -> dict[str, object]:
    if output.exists():
        raise SystemExit(f"输出 DuckDB 已存在，为避免覆盖请先指定新路径: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    sqlite_connection = sqlite_readonly()
    batch = select_batch(sqlite_connection, requested_batch)
    snapshot = sqlite_connection.execute(
        "SELECT * FROM source_snapshot WHERE source_snapshot_id=?",
        (batch["source_snapshot_id"],),
    ).fetchone()
    if not snapshot:
        raise SystemExit("批次引用的 source_snapshot 不存在")

    staging_csv = output.with_name(output.name + ".input.csv")
    if staging_csv.exists():
        staging_csv.unlink()
    query = """
        SELECT
          c.candidate_id, c.original_description, c.candidate_description,
          c.semantic_action, c.confidence, c.validator_status,
          c.reason_codes_json, c.evidence_level, c.applied_rule_ids_json,
          c.candidate_hash, c.rule_version, c.validator_version,
          d.source_row_hash, d.source_schema, d.source_asset_id,
          d.site_id, d.asset_number, d.location_code, d.location_description,
          d.location_parent, d.classification_description,
          d.class_structure_description, d.context_hash
        FROM semantic_candidate c
        JOIN device_identity d ON d.device_id=c.device_id
        WHERE c.batch_id=?
        ORDER BY c.rowid
    """
    cursor = sqlite_connection.execute(query, (batch["batch_id"],))
    rows = 0
    with staging_csv.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(FACT_COLUMNS)
        while True:
            chunk = cursor.fetchmany(5000)
            if not chunk:
                break
            writer.writerows(fact_values(row, batch) for row in chunk)
            rows += len(chunk)
    sqlite_connection.close()

    expected = int(batch["input_count"])
    if rows != expected:
        raise SystemExit(f"SQLite 批次行数异常: streamed={rows}, expected={expected}")

    duckdb, dependency_dir = load_duckdb()
    connection = duckdb.connect(str(output))
    connection.execute("PRAGMA threads=1")
    connection.execute("PRAGMA enable_progress_bar=false")
    connection.execute(
        "CREATE TABLE semantic_candidate_fact AS "
        "SELECT * FROM read_csv_auto(?, header=true, all_varchar=true, sample_size=-1, ignore_errors=false)",
        [str(staging_csv.resolve())],
    )
    connection.execute((ROOT / "duckdb_schema.sql").read_text(encoding="utf-8"))
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    baseline_id = f"sqlite-source-snapshot:{batch['source_snapshot_id']}"
    metadata = {
        "baseline_id": baseline_id,
        "batch_id": batch["batch_id"],
        "run_id": batch["run_id"],
        "source_snapshot_id": batch["source_snapshot_id"],
        "source_snapshot_hash": snapshot["snapshot_hash"],
        "input_count": str(batch["input_count"]),
        "source_write": "false",
        "formal_publication": "false",
        "builder": "build_duckdb_from_sqlite.py",
        "builder_python_abi": f"cp{sys.version_info.major}{sys.version_info.minor}",
        "duckdb_version": duckdb.__version__,
    }
    for key, value in metadata.items():
        connection.execute(
            "INSERT OR REPLACE INTO analytics_metadata VALUES (?,?,?)",
            [key, str(value), now],
        )
    fact_rows = connection.execute("SELECT count(*) FROM semantic_candidate_fact").fetchone()[0]
    distinct_candidates = connection.execute(
        "SELECT count(DISTINCT CANDIDATE_ID) FROM semantic_candidate_fact"
    ).fetchone()[0]
    distinct_identities = connection.execute(
        "SELECT count(*) FROM (SELECT DISTINCT SITEID, ASSETNUM FROM semantic_candidate_fact)"
    ).fetchone()[0]
    invalid_context = connection.execute(
        "SELECT count(*) FROM semantic_candidate_fact WHERE try_cast(CONTEXT_JSON AS JSON) IS NULL"
    ).fetchone()[0]
    site_count = connection.execute(
        "SELECT count(DISTINCT SITEID) FROM semantic_candidate_fact"
    ).fetchone()[0]
    connection.close()

    if (fact_rows, distinct_candidates, distinct_identities) != (expected, expected, expected):
        raise SystemExit(
            "新 DuckDB 与选定 SQLite 批次不一致: "
            f"facts={fact_rows}, candidates={distinct_candidates}, identities={distinct_identities}, expected={expected}"
        )
    if invalid_context:
        raise SystemExit(f"新 DuckDB 存在无效 CONTEXT_JSON: {invalid_context}")

    staging_csv.unlink(missing_ok=True)

    result = {
        "status": "built",
        "database": str(output),
        "batch_id": batch["batch_id"],
        "run_id": batch["run_id"],
        "source_snapshot_id": batch["source_snapshot_id"],
        "source_snapshot_hash": snapshot["snapshot_hash"],
        "baseline_id": baseline_id,
        "fact_rows": fact_rows,
        "distinct_candidate_ids": distinct_candidates,
        "distinct_identities": distinct_identities,
        "site_count": site_count,
        "invalid_context_json": invalid_context,
        "source_write": False,
        "formal_publication": False,
        "duckdb_runtime": str(dependency_dir),
        "duckdb_version": duckdb.__version__,
        "built_at": utc_now(),
    }
    manifest_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch-id", help="SQLite batch_id；默认选择 started_at 最新批次")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    args = parser.parse_args()
    build(args.output, args.manifest, args.batch_id)


if __name__ == "__main__":
    main()
