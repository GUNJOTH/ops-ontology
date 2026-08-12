"""Build DuckDB analytics using Python CSV chunks to avoid native CSV-reader crashes."""
from __future__ import annotations

import csv
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SEMANTIC_ROOT = ROOT.parent
INPUT = SEMANTIC_ROOT / "pilots" / "HD_SAAS" / "semantic_candidates" / "hd_semantic_candidates.csv"
MANIFEST = SEMANTIC_ROOT / "pilots" / "HD_SAAS" / "semantic_candidates" / "manifest.json"
DATA_DIR = ROOT / "data"
DUCKDB_DB = DATA_DIR / "semantic_analytics.duckdb"
DEPENDENCY_DIR = ROOT / ".deps_latest"
CHUNK_SIZE = 10000


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    sys.path.insert(0, str(DEPENDENCY_DIR))
    import duckdb  # type: ignore

    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if sha256(INPUT) != manifest["output_sha256"]:
        raise SystemExit("输入候选文件哈希不一致")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect(str(DUCKDB_DB))
    connection.execute("PRAGMA threads=1")
    connection.execute("PRAGMA enable_progress_bar=false")
    connection.execute("DROP VIEW IF EXISTS v_candidate_quality")
    connection.execute("DROP VIEW IF EXISTS v_reason_code_distribution")
    connection.execute("DROP VIEW IF EXISTS v_context_coverage")
    connection.execute("DROP VIEW IF EXISTS v_description_frequency")
    connection.execute("DROP TABLE IF EXISTS semantic_candidate_fact")

    fieldnames: list[str] | None = None
    rows = 0
    with INPUT.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = list(reader.fieldnames or [])
        if not fieldnames:
            raise SystemExit("CSV缺少表头")
        columns = ", ".join('"' + name.replace('"', '""') + '" VARCHAR' for name in fieldnames)
        connection.execute(f"CREATE TABLE semantic_candidate_fact ({columns})")
        insert_sql = "INSERT INTO semantic_candidate_fact VALUES (" + ",".join(["?"] * len(fieldnames)) + ")"
        buffer: list[list[str]] = []
        for row in reader:
            buffer.append([row.get(name, "") or "" for name in fieldnames])
            rows += 1
            if len(buffer) >= CHUNK_SIZE:
                connection.executemany(insert_sql, buffer)
                buffer.clear()
        if buffer:
            connection.executemany(insert_sql, buffer)
    connection.execute((ROOT / "duckdb_schema.sql").read_text(encoding="utf-8"))
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    metadata = {
        "input_file": str(INPUT),
        "input_sha256": sha256(INPUT),
        "batch_id": str(manifest["run_id"]),
        "rule_version": str(manifest["rule_version"]),
        "validator_version": str(manifest["validator_version"]),
        "sqlite_workflow_database": str(DATA_DIR / "semantic_workflow.sqlite3"),
    }
    for key, value in metadata.items():
        connection.execute("INSERT OR REPLACE INTO analytics_metadata VALUES (?,?,?)", [key, value, now])
    result = {
        "status": "built",
        "database": str(DUCKDB_DB),
        "fact_rows": connection.execute("SELECT count(*) FROM semantic_candidate_fact").fetchone()[0],
        "distinct_candidate_ids": connection.execute("SELECT count(DISTINCT CANDIDATE_ID) FROM semantic_candidate_fact").fetchone()[0],
        "distinct_identities": connection.execute("SELECT count(*) FROM (SELECT DISTINCT SITEID,ASSETNUM FROM semantic_candidate_fact)").fetchone()[0],
        "source_write": False,
        "formal_publication": False,
    }
    connection.close()
    (DATA_DIR / "duckdb_initialization.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
