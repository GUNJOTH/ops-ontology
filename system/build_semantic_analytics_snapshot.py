"""Build the local DuckDB analytics companion for a bounded identity snapshot.

The old ``semantic_candidate_fact`` table belongs to the device-description
workflow and must not be populated with identity rows.  This builder creates
an explicit ``semantic_device_identity_fact`` projection instead, while
keeping the legacy candidate table empty and preserving the source-read-only
boundary.
"""
from __future__ import annotations

import argparse
import csv
import json
import pathlib
import sqlite3
import sys
from datetime import datetime, timezone

from pipeline.contracts import connect_readonly

ROOT = pathlib.Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parent
DATA_DIR = ROOT / "data"
DEFAULT_IDENTITY = PROJECT_ROOT / "pilots" / "identity" / "results"
DEFAULT_OUTPUT = DATA_DIR / "semantic_analytics_v155.duckdb"

IDENTITY_COLUMNS = (
    "unified_device_id", "source_snapshot_id", "source_schema", "site_id",
    "asset_number", "source_identity_key", "canonical_name", "description_norm",
    "location_code", "parent_asset_number", "org_id", "classstructure_id",
    "classification_id", "status", "identity_scope", "seed_status",
    "context_json", "created_at",
)

LEGACY_FACT_COLUMNS = (
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


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_duckdb():
    abi = f"cp{sys.version_info.major}{sys.version_info.minor}"
    for dependency_dir in (PROJECT_ROOT / ".deps", PROJECT_ROOT / "backend" / ".deps"):
        duckdb_dir = dependency_dir / "duckdb"
        candidates = list(duckdb_dir.glob(f"*{abi}-win_amd64.pyd")) if duckdb_dir.exists() else []
        candidates.extend(dependency_dir.glob(f"_*{abi}-win_amd64.pyd"))
        if not candidates:
            continue
        sys.path.insert(0, str(dependency_dir))
        import duckdb  # type: ignore

        return duckdb
    raise RuntimeError(f"未找到适用于 Python {abi} 的 DuckDB 运行时")


def latest_identity_db(root: pathlib.Path) -> pathlib.Path:
    paths = sorted(root.glob("identity-layer-v1-*/identity_semantics.sqlite3"), reverse=True)
    if not paths:
        raise FileNotFoundError(f"没有找到身份结果库: {root}")
    return paths[0]


def read_identity(path: pathlib.Path) -> list[dict[str, object]]:
    connection = connect_readonly(path.resolve(), timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    try:
        source_columns = [
            "unified_device_id", "source_snapshot_id",
            "master_source_schema AS source_schema", "site_id", "asset_number",
            "source_identity_key", "canonical_name", "description_norm",
            "location_code", "parent_asset_number", "org_id", "classstructure_id",
            "classification_id", "status", "identity_scope", "seed_status",
            "created_at",
        ]
        rows = connection.execute(
            "SELECT " + ",".join(source_columns) + " FROM unified_device ORDER BY unified_device_id"
        ).fetchall()
        result: list[dict[str, object]] = []
        for row in rows:
            payload = dict(row)
            payload["context_json"] = json.dumps(
                {
                    "identity": {
                        "source_schema": payload.get("source_schema"),
                        "site_id": payload.get("site_id"),
                        "asset_number": payload.get("asset_number"),
                    },
                    "location": {"LOCATION": payload.get("location_code") or ""},
                    "source_snapshot_id": payload.get("source_snapshot_id"),
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
            result.append(payload)
        return result
    finally:
        connection.close()


def build(identity_db: pathlib.Path, output: pathlib.Path) -> dict[str, object]:
    if output.exists():
        raise FileExistsError(f"输出 DuckDB 已存在，为避免覆盖请先移除测试文件: {output}")
    rows = read_identity(identity_db)
    if not rows:
        raise RuntimeError("身份结果库没有 unified_device 记录")
    duckdb = load_duckdb()
    output.parent.mkdir(parents=True, exist_ok=True)
    staging_csv = output.with_name(output.name + ".identity.csv")
    with staging_csv.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(IDENTITY_COLUMNS)
        writer.writerows(
            [None if row.get(column) is None else str(row.get(column)) for column in IDENTITY_COLUMNS]
            for row in rows
        )
    connection = duckdb.connect(str(output))
    try:
        connection.execute("PRAGMA threads=1")
        connection.execute("PRAGMA enable_progress_bar=false")
        connection.execute("CREATE TABLE analytics_metadata (metadata_key VARCHAR PRIMARY KEY, metadata_value VARCHAR NOT NULL, updated_at TIMESTAMP NOT NULL)")
        connection.execute("CREATE TABLE batch_quality_metric (batch_id VARCHAR NOT NULL, metric_group VARCHAR NOT NULL, metric_name VARCHAR NOT NULL, metric_value DOUBLE NOT NULL, dimension_json JSON, measured_at TIMESTAMP NOT NULL, PRIMARY KEY(batch_id, metric_group, metric_name))")
        legacy_defs = ",".join(f'"{column}" VARCHAR' for column in LEGACY_FACT_COLUMNS)
        connection.execute(f"CREATE TABLE semantic_candidate_fact ({legacy_defs})")
        connection.execute(
            "CREATE TABLE semantic_device_identity_fact AS "
            "SELECT * FROM read_csv_auto(?, header=true, all_varchar=true, sample_size=-1, ignore_errors=false)",
            [str(staging_csv.resolve())],
        )
        connection.execute("CREATE OR REPLACE VIEW v_identity_snapshot_quality AS SELECT source_schema,source_snapshot_id,count(*) AS device_count,count(DISTINCT site_id) AS site_count,count(*) FILTER (WHERE trim(coalesce(asset_number,''))<>'') AS asset_number_count FROM semantic_device_identity_fact GROUP BY source_schema,source_snapshot_id")
        timestamp = datetime.now(timezone.utc).replace(tzinfo=None)
        snapshot_ids = sorted({str(row.get("source_snapshot_id") or "") for row in rows})
        metadata = {
            "baseline_id": "identity-source-snapshot:" + ",".join(snapshot_ids),
            "source_snapshot_id": ",".join(snapshot_ids),
            "identity_device_count": str(len(rows)),
            "legacy_candidate_fact_rows": "0",
            "source_write": "false",
            "formal_publication": "false",
            "builder": "build_semantic_analytics_snapshot.py",
            "duckdb_version": duckdb.__version__,
        }
        connection.executemany(
            "INSERT INTO analytics_metadata VALUES (?,?,?)",
            [[key, value, timestamp] for key, value in metadata.items()],
        )
    finally:
        connection.close()
    staging_csv.unlink(missing_ok=True)
    result = {
        "status": "built",
        "database": str(output),
        "identity_source": str(identity_db),
        "identity_snapshot_ids": snapshot_ids,
        "identity_device_count": len(rows),
        "legacy_candidate_fact_rows": 0,
        "source_write": False,
        "formal_publication": False,
        "built_at": now(),
    }
    manifest = output.with_suffix(output.suffix + ".manifest.json")
    manifest.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a DuckDB analytics companion for a local identity snapshot")
    parser.add_argument("--identity-db", type=pathlib.Path)
    parser.add_argument("--identity-root", type=pathlib.Path, default=DEFAULT_IDENTITY)
    parser.add_argument("--output", type=pathlib.Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    identity_db = args.identity_db.resolve() if args.identity_db else latest_identity_db(args.identity_root.resolve())
    print(json.dumps(build(identity_db, args.output.resolve()), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()