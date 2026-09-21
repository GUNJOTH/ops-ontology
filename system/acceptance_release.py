"""Verify, mark, and back up the current formal publication release."""
from __future__ import annotations

import json
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
BACKUP_ROOT = ROOT / "backups"
SQLITE_DB = DATA_DIR / "semantic_workflow.sqlite3"
DUCKDB_DB = DATA_DIR / "semantic_analytics_v155.duckdb"
DUCKDB_WAL = DATA_DIR / "semantic_analytics_v155.duckdb.wal"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def main() -> None:
    now = utc_now()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_dir = BACKUP_ROOT / f"formal-release-{stamp}"
    backup_dir.mkdir(parents=True, exist_ok=False)

    connection = sqlite3.connect(str(SQLITE_DB), timeout=60)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    batch = connection.execute("SELECT * FROM batch_run ORDER BY started_at DESC LIMIT 1").fetchone()
    if batch is None:
        raise SystemExit("No batch found.")

    checks = {
        "published_count": int(connection.execute("SELECT count(*) FROM published_description").fetchone()[0]),
        "distinct_identities": int(connection.execute(
            "SELECT count(*) FROM (SELECT DISTINCT source_schema,site_id,asset_number FROM published_description)"
        ).fetchone()[0]),
        "missing_approval": int(connection.execute(
            "SELECT count(*) FROM published_description p "
            "LEFT JOIN review_decision r ON r.review_id=p.review_id "
            "WHERE r.review_id IS NULL OR r.decision NOT IN ('approved','modified')"
        ).fetchone()[0]),
        "missing_replay": int(connection.execute(
            "SELECT count(*) FROM published_description p "
            "LEFT JOIN replay_run rr ON rr.replay_id=p.replay_id "
            "WHERE rr.replay_id IS NULL OR rr.status!='passed'"
        ).fetchone()[0]),
        "candidate_published": int(connection.execute(
            "SELECT count(*) FROM semantic_candidate WHERE publication_state='published'"
        ).fetchone()[0]),
        "source_write": int(connection.execute(
            "SELECT coalesce(max(source_write),0) FROM batch_run WHERE batch_id=?", (batch["batch_id"],)
        ).fetchone()[0]),
    }
    expected = int(batch["published_count"])
    failures = []
    if checks["published_count"] != expected:
        failures.append(f"published_count={checks['published_count']} expected={expected}")
    if checks["distinct_identities"] != expected:
        failures.append("duplicate_formal_identity")
    if checks["missing_approval"] or checks["missing_replay"]:
        failures.append("missing_provenance")
    if checks["candidate_published"] != expected:
        failures.append("candidate_publication_state_mismatch")
    if checks["source_write"] != 0 or int(batch["source_write"]) != 0:
        failures.append("SOURCE_WRITE_TRUE")
    if failures:
        connection.close()
        raise SystemExit(json.dumps({"status": "BLOCKED", "failures": failures}, ensure_ascii=False))

    payload = {
        "batch_id": batch["batch_id"],
        "published_count": expected,
        "checks": checks,
        "source_write": False,
        "formal_publication": bool(batch["formal_publication"]),
        "verified_at": now,
        "backup_dir": str(backup_dir),
    }
    connection.execute(
        "INSERT INTO audit_event(entity_type,entity_id,event_type,actor,payload_json,event_at) VALUES (?,?,?,?,?,?)",
        ("publication", batch["batch_id"], "publication_acceptance_verified", "semantic-release-check", json.dumps(payload, ensure_ascii=False), now),
    )
    connection.execute(
        "INSERT INTO system_setting(setting_key,setting_value,updated_at) VALUES (?,?,?) "
        "ON CONFLICT(setting_key) DO UPDATE SET setting_value=excluded.setting_value,updated_at=excluded.updated_at",
        ("latest_formal_publication_batch", json.dumps(payload, ensure_ascii=False), now),
    )
    connection.commit()

    sqlite_backup = backup_dir / SQLITE_DB.name
    backup_connection = sqlite3.connect(str(sqlite_backup))
    connection.backup(backup_connection)
    backup_connection.close()
    connection.close()

    duckdb_backup = backup_dir / DUCKDB_DB.name
    shutil.copy2(DUCKDB_DB, duckdb_backup)
    backup_files = [sqlite_backup, duckdb_backup]
    if DUCKDB_WAL.exists():
        duckdb_wal_backup = backup_dir / DUCKDB_WAL.name
        shutil.copy2(DUCKDB_WAL, duckdb_wal_backup)
        backup_files.append(duckdb_wal_backup)

    manifest = {
        **payload,
        "status": "accepted_and_backed_up",
        "files": [
            {"path": str(path), "size": path.stat().st_size} for path in backup_files
        ],
    }
    (backup_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
