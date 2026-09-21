"""Load the validated metadata dictionary into local SQLite and DuckDB layers."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import pathlib
import sqlite3
from datetime import datetime, timezone

import duckdb


def quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def read_csv(path: pathlib.Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fields = list(reader.fieldnames or [])
        return fields, [{field: "" if row.get(field) is None else str(row.get(field)) for field in fields} for row in reader]


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]


def create_sqlite(path: pathlib.Path, fields: list[str], rows: list[dict[str, str]], findings: list[dict[str, str]], version: str, loaded_at: str) -> int:
    connection = sqlite3.connect(str(path))
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        columns = ["semantic_id TEXT PRIMARY KEY"] + [f"{quote_identifier(field)} TEXT NOT NULL DEFAULT ''" for field in fields]
        connection.execute(f"CREATE TABLE metadata_semantic_dictionary ({', '.join(columns)}, loaded_at TEXT NOT NULL)")
        connection.execute("CREATE TABLE metadata_semantic_run (run_id TEXT PRIMARY KEY, dictionary_version TEXT NOT NULL, row_count INTEGER NOT NULL, loaded_at TEXT NOT NULL, source_write INTEGER NOT NULL, formal_publication INTEGER NOT NULL)")
        finding_fields = list(findings[0]) if findings else ["finding_id", "finding_type", "concept_type", "semantic_key", "source_schema", "severity", "message"]
        finding_columns = [f"{quote_identifier(field)} TEXT NOT NULL DEFAULT ''" for field in finding_fields]
        connection.execute(f"CREATE TABLE metadata_validation_findings ({', '.join(finding_columns)})")
        insert_fields = ["semantic_id", *fields, "loaded_at"]
        marks = ",".join("?" for _ in insert_fields)
        insert_sql = f"INSERT INTO metadata_semantic_dictionary ({','.join(quote_identifier(field) for field in insert_fields)}) VALUES ({marks})"
        data = []
        for row in rows:
            semantic_id = digest(f"{row.get('concept_type', '')}|{row.get('semantic_key', '')}")
            data.append([semantic_id, *[row.get(field, "") for field in fields], loaded_at])
        connection.executemany(insert_sql, data)
        finding_sql = f"INSERT INTO metadata_validation_findings ({','.join(quote_identifier(field) for field in finding_fields)}) VALUES ({','.join('?' for _ in finding_fields)})"
        connection.executemany(finding_sql, [[row.get(field, "") for field in finding_fields] for row in findings])
        run_id = f"metadata-semantic-local-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
        connection.execute("INSERT INTO metadata_semantic_run VALUES (?,?,?,?,?,?)", (run_id, version, len(rows), loaded_at, 0, 0))
        connection.execute("CREATE INDEX ix_metadata_semantic_type_key ON metadata_semantic_dictionary(concept_type, semantic_key)")
        connection.execute("CREATE INDEX ix_metadata_semantic_status ON metadata_semantic_dictionary(semantic_status, ai_category)")
        connection.execute("CREATE INDEX ix_metadata_semantic_name ON metadata_semantic_dictionary(canonical_name)")
        connection.commit()
        count = int(connection.execute("SELECT count(*) FROM metadata_semantic_dictionary").fetchone()[0])
        return count
    finally:
        connection.close()


def create_duckdb(path: pathlib.Path, dictionary_csv: pathlib.Path, findings_csv: pathlib.Path, version: str, loaded_at: str) -> int:
    connection = duckdb.connect(str(path))
    try:
        dictionary_literal = str(dictionary_csv).replace("\\", "/").replace("'", "''")
        findings_literal = str(findings_csv).replace("\\", "/").replace("'", "''")
        connection.execute(
            """
            CREATE TABLE metadata_semantic_dictionary AS
            SELECT sha256(concat(coalesce(concept_type, ''), '|', coalesce(semantic_key, ''))) AS semantic_id,
                   *
            FROM read_csv_auto(?, header=true, all_varchar=true, union_by_name=true)
            """,
            [dictionary_literal],
        )
        connection.execute("ALTER TABLE metadata_semantic_dictionary ADD COLUMN loaded_at VARCHAR")
        connection.execute("UPDATE metadata_semantic_dictionary SET loaded_at=?", [loaded_at])
        connection.execute("CREATE TABLE metadata_semantic_run (run_id VARCHAR, dictionary_version VARCHAR, row_count BIGINT, loaded_at VARCHAR, source_write BOOLEAN, formal_publication BOOLEAN)")
        connection.execute(
            """
            CREATE TABLE metadata_validation_findings AS
            SELECT * FROM read_csv_auto(?, header=true, all_varchar=true, union_by_name=true)
            """,
            [findings_literal],
        )
        run_id = f"metadata-semantic-local-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
        row_count = int(connection.execute("SELECT count(*) FROM metadata_semantic_dictionary").fetchone()[0])
        connection.execute("INSERT INTO metadata_semantic_run VALUES (?,?,?,?,?,?)", (run_id, version, row_count, loaded_at, False, False))
        connection.execute("CREATE INDEX ix_metadata_semantic_type_key ON metadata_semantic_dictionary(concept_type, semantic_key)")
        connection.execute("CREATE INDEX ix_metadata_semantic_status ON metadata_semantic_dictionary(semantic_status, ai_category)")
        connection.execute("CREATE INDEX ix_metadata_semantic_name ON metadata_semantic_dictionary(canonical_name)")
        return row_count
    finally:
        connection.close()


def create_empty_duckdb(path: pathlib.Path, dictionary_fields: list[str], finding_fields: list[str], version: str, loaded_at: str) -> int:
    """Create an explicit empty baseline without inventing metadata rows."""
    connection = duckdb.connect(str(path))
    try:
        dictionary_columns = ["semantic_id VARCHAR"] + [f"{quote_identifier(field)} VARCHAR" for field in dictionary_fields] + ["loaded_at VARCHAR"]
        connection.execute(f"CREATE TABLE metadata_semantic_dictionary ({', '.join(dictionary_columns)})")
        finding_columns = [f"{quote_identifier(field)} VARCHAR" for field in finding_fields]
        connection.execute(f"CREATE TABLE metadata_validation_findings ({', '.join(finding_columns)})")
        connection.execute("CREATE TABLE metadata_semantic_run (run_id VARCHAR, dictionary_version VARCHAR, row_count BIGINT, loaded_at VARCHAR, source_write BOOLEAN, formal_publication BOOLEAN)")
        run_id = f"metadata-semantic-empty-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
        connection.execute("INSERT INTO metadata_semantic_run VALUES (?,?,?,?,?,?)", (run_id, version, 0, loaded_at, False, False))
        connection.execute("CREATE INDEX ix_metadata_semantic_type_key ON metadata_semantic_dictionary(concept_type, semantic_key)")
        connection.execute("CREATE INDEX ix_metadata_semantic_status ON metadata_semantic_dictionary(semantic_status, ai_category)")
        connection.execute("CREATE INDEX ix_metadata_semantic_name ON metadata_semantic_dictionary(canonical_name)")
        return 0
    finally:
        connection.close()


def create_empty_layer(output_dir: pathlib.Path, version: str) -> dict[str, object]:
    """Bootstrap a truthful zero-row local result layer for offline development."""
    output_dir.mkdir(parents=True, exist_ok=True)
    sqlite_path = output_dir / "metadata_semantics.sqlite3"
    duckdb_path = output_dir / "metadata_semantics.duckdb"
    if sqlite_path.exists() or duckdb_path.exists():
        raise FileExistsError(f"元数据结果层已存在：{output_dir}")
    dictionary_fields = [
        "dictionary_version", "concept_type", "semantic_key", "canonical_name", "semantic_label_candidate",
        "description", "data_type", "length", "required", "domain_id", "parent_or_table", "source_schemas",
        "cross_schema_status", "ai_category", "ai_confidence", "ai_reason", "semantic_status", "evidence",
    ]
    finding_fields = ["finding_id", "finding_type", "concept_type", "semantic_key", "source_schema", "severity", "message"]
    loaded_at = datetime.now(timezone.utc).isoformat()
    sqlite_count = create_sqlite(sqlite_path, dictionary_fields, [], [], version, loaded_at)
    duckdb_count = create_empty_duckdb(duckdb_path, dictionary_fields, finding_fields, version, loaded_at)
    manifest = {
        "run_id": output_dir.name, "generated_at": loaded_at, "status": "empty_baseline",
        "dictionary_version": version, "input_validation_dir": None, "input_count": 0,
        "sqlite_file": str(sqlite_path), "duckdb_file": str(duckdb_path), "sqlite_count": sqlite_count,
        "duckdb_count": duckdb_count, "isolated_findings_loaded": 0,
        "source_write": False, "formal_publication": False,
        "note": "仅用于本地接口和测试，未伪造任何源系统元数据。",
    }
    (output_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description="Load validated metadata semantic dictionary locally")
    parser.add_argument("--validation-dir", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--empty-baseline", action="store_true", help="建立无数据的本地基线，不伪造元数据")
    parser.add_argument("--version", default="metadata-semantic-empty-v1")
    args = parser.parse_args()
    validation_dir = pathlib.Path(args.validation_dir).resolve()
    output_dir = pathlib.Path(args.output_dir).resolve()
    if args.empty_baseline:
        print(json.dumps(create_empty_layer(output_dir, args.version), ensure_ascii=False))
        return
    output_dir.mkdir(parents=True, exist_ok=False)
    dictionary_path = validation_dir / "formal_ready_metadata_semantic_dictionary.csv"
    findings_path = validation_dir / "metadata_validation_findings.csv"
    fields, rows = read_csv(dictionary_path)
    _, findings = read_csv(findings_path)
    version = rows[0].get("dictionary_version", output_dir.name) if rows else output_dir.name
    loaded_at = datetime.now(timezone.utc).isoformat()
    sqlite_path = output_dir / "metadata_semantics.sqlite3"
    duckdb_path = output_dir / "metadata_semantics.duckdb"
    sqlite_count = create_sqlite(sqlite_path, fields, rows, findings, version, loaded_at)
    duckdb_count = create_duckdb(duckdb_path, dictionary_path, findings_path, version, loaded_at)
    manifest = {
        "run_id": output_dir.name,
        "generated_at": loaded_at,
        "status": "local_result_layer_ready",
        "dictionary_version": version,
        "input_validation_dir": str(validation_dir),
        "input_count": len(rows),
        "sqlite_file": str(sqlite_path),
        "duckdb_file": str(duckdb_path),
        "sqlite_count": sqlite_count,
        "duckdb_count": duckdb_count,
        "isolated_findings_loaded": len(findings),
        "source_write": False,
        "formal_publication": False,
        "next_gate": "expose read-only metadata semantic query API and page",
    }
    (output_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False))


if __name__ == "__main__":
    main()
