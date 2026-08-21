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


def main() -> None:
    parser = argparse.ArgumentParser(description="Load validated metadata semantic dictionary locally")
    parser.add_argument("--validation-dir", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()
    validation_dir = pathlib.Path(args.validation_dir).resolve()
    output_dir = pathlib.Path(args.output_dir).resolve()
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
