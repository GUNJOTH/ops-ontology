"""Verify row counts and uniqueness in the local metadata result layer."""
from __future__ import annotations

import argparse
import json
import pathlib
import sqlite3

import duckdb


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify local metadata semantic result layer")
    parser.add_argument("--result-dir", required=True)
    args = parser.parse_args()
    result_dir = pathlib.Path(args.result_dir).resolve()
    sqlite = sqlite3.connect(result_dir / "metadata_semantics.sqlite3")
    duck = duckdb.connect(str(result_dir / "metadata_semantics.duckdb"), read_only=True)
    try:
        sqlite_count = int(sqlite.execute("SELECT count(*) FROM metadata_semantic_dictionary").fetchone()[0])
        sqlite_duplicates = int(sqlite.execute("SELECT count(*) FROM (SELECT concept_type,semantic_key FROM metadata_semantic_dictionary GROUP BY concept_type,semantic_key HAVING count(*) > 1)").fetchone()[0])
        duck_count = int(duck.execute("SELECT count(*) FROM metadata_semantic_dictionary").fetchone()[0])
        duck_duplicates = int(duck.execute("SELECT count(*) FROM (SELECT concept_type,semantic_key FROM metadata_semantic_dictionary GROUP BY concept_type,semantic_key HAVING count(*) > 1)").fetchone()[0])
        concept_types = dict(duck.execute("SELECT concept_type,count(*) FROM metadata_semantic_dictionary GROUP BY concept_type ORDER BY concept_type").fetchall())
        ai_categories = dict(duck.execute("SELECT coalesce(ai_category,''),count(*) FROM metadata_semantic_dictionary GROUP BY ai_category ORDER BY ai_category").fetchall())
        report = {
            "status": "passed" if sqlite_count == duck_count and sqlite_duplicates == 0 and duck_duplicates == 0 else "failed",
            "sqlite_count": sqlite_count,
            "duckdb_count": duck_count,
            "sqlite_duplicate_keys": sqlite_duplicates,
            "duckdb_duplicate_keys": duck_duplicates,
            "concept_types": concept_types,
            "ai_categories": ai_categories,
        }
        (result_dir / "verification.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        manifest_path = result_dir / "manifest.json"
        if manifest_path.exists():
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["verification_status"] = report["status"]
            manifest["verification_file"] = str(result_dir / "verification.json")
            manifest["verified_sqlite_count"] = sqlite_count
            manifest["verified_duckdb_count"] = duck_count
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False))
        if report["status"] != "passed":
            raise SystemExit(1)
    finally:
        sqlite.close()
        duck.close()


if __name__ == "__main__":
    main()
