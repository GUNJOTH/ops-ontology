"""Build DuckDB analytics with the native CSV reader and an ASCII alias path."""
from __future__ import annotations

import hashlib
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SEMANTIC_ROOT = ROOT.parent
INPUT = SEMANTIC_ROOT / "pilots" / "HD_SAAS" / "semantic_candidates" / "hd_semantic_candidates.csv"
MANIFEST = SEMANTIC_ROOT / "pilots" / "HD_SAAS" / "semantic_candidates" / "manifest.json"
DATA_DIR = ROOT / "data"
DUCKDB_DB = DATA_DIR / "semantic_analytics.duckdb"
INPUT_ALIAS_DIR = DATA_DIR / "input_alias"
INPUT_ALIAS = INPUT_ALIAS_DIR / "semantic_candidates.csv"
DEPENDENCY_DIR = SEMANTIC_ROOT / "backend" / ".deps"


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
    INPUT_ALIAS_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(INPUT, INPUT_ALIAS)
    db_path = DATA_DIR / "semantic_analytics_v155.duckdb"
    if db_path.exists():
        db_path.unlink()
    wal = Path(str(db_path) + ".wal")
    if wal.exists():
        wal.unlink()
    connection = duckdb.connect(str(db_path))
    connection.execute("PRAGMA threads=4")
    alias = str(INPUT_ALIAS).replace("\\", "/").replace("'", "''")
    connection.execute(
        f"CREATE TABLE semantic_candidate_fact AS SELECT * FROM read_csv_auto('{alias}', header=true, all_varchar=true, sample_size=-1, ignore_errors=false)"
    )
    connection.execute((ROOT / "duckdb_schema.sql").read_text(encoding="utf-8"))
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    metadata = {
        "input_file": str(INPUT),
        "input_alias": str(INPUT_ALIAS),
        "input_sha256": sha256(INPUT),
        "batch_id": str(manifest["run_id"]),
        "rule_version": str(manifest["rule_version"]),
        "validator_version": str(manifest["validator_version"]),
        "duckdb_version": duckdb.__version__,
    }
    for key, value in metadata.items():
        connection.execute("INSERT OR REPLACE INTO analytics_metadata VALUES (?,?,?)", [key, value, now])
    result = {
        "status": "built",
        "database": str(db_path),
        "duckdb_version": duckdb.__version__,
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
