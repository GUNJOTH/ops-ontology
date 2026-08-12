"""Initialize SQLite workflow storage and DuckDB analytical storage."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SEMANTIC_ROOT = ROOT.parent
DEFAULT_CSV = SEMANTIC_ROOT / "pilots" / "HD_SAAS" / "semantic_candidates" / "hd_semantic_candidates.csv"
DEFAULT_MANIFEST = SEMANTIC_ROOT / "pilots" / "HD_SAAS" / "semantic_candidates" / "manifest.json"
DATA_DIR = ROOT / "data"
SQLITE_DB = DATA_DIR / "semantic_workflow.sqlite3"
DUCKDB_DB = DATA_DIR / "semantic_analytics_v155.duckdb"
DEPENDENCY_DIR = ROOT / ".deps_latest"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_duckdb():
    if DEPENDENCY_DIR.exists():
        sys.path.insert(0, str(DEPENDENCY_DIR))
    try:
        import duckdb  # type: ignore
    except ModuleNotFoundError as exc:
        raise SystemExit(
            "缺少 duckdb。请执行：D:\\uv\\bin\\uv.exe pip install --target "
            f'"{DEPENDENCY_DIR}" -r "{ROOT / "requirements.txt"}"'
        ) from exc
    return duckdb


def initialize_sqlite(csv_path: Path, manifest: dict[str, object]) -> dict[str, object]:
    sqlite_path = SQLITE_DB
    connection = sqlite3.connect(sqlite_path)
    connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript((ROOT / "sqlite_schema.sql").read_text(encoding="utf-8"))
    now = utc_now()
    snapshot_id = str(manifest["run_id"]).replace("hd-semantic-candidate-generation-", "")
    source_snapshot_id = ""
    with csv_path.open(encoding="utf-8-sig", newline="") as handle:
        first = next(csv.DictReader(handle))
        source_snapshot_id = first["SOURCE_SNAPSHOT_ID"]
    connection_id = "maxieam-hd-saas-readonly"
    batch_id = str(manifest["run_id"])
    connection.execute(
        "INSERT OR IGNORE INTO source_connection VALUES (?,?,?,?,?,?,?,?,?)",
        (connection_id, "MaxiEAM", "HD_SAAS", "env:HD_DM_DSN", 0, "read_only", "env:HD_DM_PASSWORD", 1, now),
    )
    connection.execute(
        "INSERT OR REPLACE INTO source_snapshot "
        "(source_snapshot_id,connection_id,source_table,source_filter,source_row_count,distinct_identity_count,snapshot_hash,snapshot_path,source_write,captured_at,status) "
        "VALUES (?,?,?,?,?,?,?,?,0,?,'verified')",
        (
            source_snapshot_id,
            connection_id,
            "HD_SAAS.ASSET + explicit context tables",
            "quality scope after non-operational, missing-location, low-overlap and suspect-description exclusions",
            int(manifest["output_rows"]),
            int(manifest["output_rows"]),
            str(manifest["input_sha256"]),
            str(manifest["input_file"]),
            now,
        ),
    )
    connection.execute(
        "INSERT OR REPLACE INTO batch_run "
        "(batch_id,run_id,source_snapshot_id,batch_type,rule_version,validator_version,input_count,candidate_count,needs_review_count,blocked_count,failed_count,status,config_json,started_at,finished_at,source_write,formal_publication) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,0,'completed',?,?,?,0,0)",
        (
            batch_id,
            batch_id,
            source_snapshot_id,
            "semantic_candidate_generation",
            str(manifest["rule_version"]),
            str(manifest["validator_version"]),
            int(manifest["input_rows"]),
            int(manifest["output_rows"]),
            0,
            0,
            json.dumps({"input_sha256": manifest["input_sha256"], "output_sha256": manifest["output_sha256"]}),
            now,
            now,
        ),
    )

    connection.execute("DELETE FROM semantic_candidate WHERE batch_id=?", (batch_id,))
    connection.execute("DELETE FROM device_identity WHERE source_snapshot_id=? AND source_schema='HD_SAAS'", (source_snapshot_id,))
    device_sql = (
        "INSERT INTO device_identity "
        "(source_snapshot_id,source_schema,source_asset_id,site_id,asset_number,source_row_hash,original_description,location_code,location_description,location_parent,classification_description,class_structure_description,context_hash,analytics_row_key,created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
    )
    candidate_sql = (
        "INSERT INTO semantic_candidate "
        "(candidate_id,batch_id,device_id,original_description,candidate_description,semantic_action,confidence,validator_status,reason_codes_json,evidence_level,applied_rule_ids_json,candidate_hash,rule_version,validator_version,created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
    )
    inserted = 0
    with csv_path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            context = json.loads(row.get("CONTEXT_JSON") or "{}")
            location_code = str((context.get("location") or {}).get("LOCATION") or "").strip()
            reason_codes = [item for item in (row.get("SEMANTIC_REASON_CODES") or "").split("|") if item]
            applied_rules = [item for item in (row.get("APPLIED_TERM_RULE_IDS") or "").split("|") if item]
            cursor = connection.execute(
                device_sql,
                (
                    source_snapshot_id,
                    "HD_SAAS",
                    row.get("ASSETID"),
                    row["SITEID"],
                    row["ASSETNUM"],
                    row["SOURCE_ROW_HASH"],
                    row["ORIGINAL_DESCRIPTION"],
                    location_code,
                    row.get("LOCATION_DESCRIPTION"),
                    row.get("LOCATION_PARENT"),
                    row.get("CLASSIFICATION_DESCRIPTION"),
                    row.get("CLASSSTRUCTURE_DESCRIPTION"),
                    row["CONTEXT_HASH"],
                    row["CANDIDATE_ID"],
                    now,
                ),
            )
            connection.execute(
                candidate_sql,
                (
                    row["CANDIDATE_ID"],
                    batch_id,
                    cursor.lastrowid,
                    row["ORIGINAL_DESCRIPTION"],
                    row["UNIFIED_DESCRIPTION"],
                    row["SEMANTIC_ACTION"],
                    row["SEMANTIC_CONFIDENCE"],
                    row["SEMANTIC_RESULT_STATUS"],
                    json.dumps(reason_codes, ensure_ascii=False),
                    row["CONTEXT_EVIDENCE_LEVEL"],
                    json.dumps(applied_rules, ensure_ascii=False),
                    row["SEMANTIC_CANDIDATE_HASH"],
                    row["SEMANTIC_RULE_VERSION"],
                    row["SEMANTIC_VALIDATOR_VERSION"],
                    now,
                ),
            )
            inserted += 1
            if inserted % 10000 == 0:
                connection.commit()
    connection.execute(
        "INSERT INTO audit_event(entity_type,entity_id,event_type,actor,payload_json,event_at) VALUES (?,?,?,?,?,?)",
        ("batch", batch_id, "batch_imported", "init_system.py", json.dumps({"rows": inserted, "csv_sha256": sha256(csv_path)}), now),
    )
    connection.commit()
    result = {
        "database": str(sqlite_path),
        "batch_id": batch_id,
        "source_snapshot_id": source_snapshot_id,
        "device_rows": connection.execute("SELECT count(*) FROM device_identity WHERE source_snapshot_id=?", (source_snapshot_id,)).fetchone()[0],
        "candidate_rows": connection.execute("SELECT count(*) FROM semantic_candidate WHERE batch_id=?", (batch_id,)).fetchone()[0],
    }
    connection.close()
    return result


def initialize_duckdb(csv_path: Path, manifest: dict[str, object]) -> dict[str, object]:
    duckdb = load_duckdb()
    connection = duckdb.connect(str(DUCKDB_DB))
    escaped_path = str(csv_path).replace("'", "''")
    connection.execute("DROP VIEW IF EXISTS v_candidate_quality")
    connection.execute("DROP VIEW IF EXISTS v_reason_code_distribution")
    connection.execute("DROP VIEW IF EXISTS v_context_coverage")
    connection.execute("DROP VIEW IF EXISTS v_description_frequency")
    connection.execute("DROP TABLE IF EXISTS semantic_candidate_fact")
    connection.execute(
        "CREATE TABLE semantic_candidate_fact AS "
        f"SELECT * FROM read_csv_auto('{escaped_path}', header=true, all_varchar=true, sample_size=-1, ignore_errors=false)"
    )
    connection.execute((ROOT / "duckdb_schema.sql").read_text(encoding="utf-8"))
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    metadata = {
        "input_file": str(csv_path),
        "input_sha256": sha256(csv_path),
        "batch_id": str(manifest["run_id"]),
        "rule_version": str(manifest["rule_version"]),
        "validator_version": str(manifest["validator_version"]),
        "sqlite_workflow_database": str(SQLITE_DB),
    }
    for key, value in metadata.items():
        connection.execute("INSERT OR REPLACE INTO analytics_metadata VALUES (?,?,?)", [key, value, now])
    row_count = connection.execute("SELECT count(*) FROM semantic_candidate_fact").fetchone()[0]
    distinct_candidates = connection.execute("SELECT count(DISTINCT CANDIDATE_ID) FROM semantic_candidate_fact").fetchone()[0]
    distinct_identities = connection.execute("SELECT count(*) FROM (SELECT DISTINCT SITEID,ASSETNUM FROM semantic_candidate_fact)").fetchone()[0]
    connection.close()
    return {
        "database": str(DUCKDB_DB),
        "fact_rows": row_count,
        "distinct_candidate_ids": distinct_candidates,
        "distinct_identities": distinct_identities,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--duckdb-only", action="store_true", help="SQLite 已完成时只重建 DuckDB 分析库")
    args = parser.parse_args()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not args.csv.exists() or not args.manifest.exists():
        raise SystemExit("缺少当前语义候选 CSV 或 manifest")
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    actual_hash = sha256(args.csv)
    if actual_hash != manifest["output_sha256"]:
        raise SystemExit(f"候选 CSV 哈希不一致: {actual_hash} != {manifest['output_sha256']}")
    if args.duckdb_only:
        sqlite_result = {"database": str(SQLITE_DB), "status": "skipped_existing_verified_sqlite"}
    else:
        sqlite_result = initialize_sqlite(args.csv, manifest)
    duckdb_result = initialize_duckdb(args.csv, manifest)
    result = {
        "status": "initialized",
        "sqlite": sqlite_result,
        "duckdb": duckdb_result,
        "source_write": False,
        "formal_publication": False,
    }
    (DATA_DIR / "initialization.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
