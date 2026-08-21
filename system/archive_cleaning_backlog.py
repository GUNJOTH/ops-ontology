"""Archive stale replayed cleaning tasks without deleting workflow evidence."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "system" / "data"
BACKUP_ROOT = ROOT / "system" / "backups"
DB_PATH = DATA_DIR / "semantic_workflow.sqlite3"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_columns(connection: sqlite3.Connection) -> None:
    columns = {row[1] for row in connection.execute("PRAGMA table_info(cleaning_run)").fetchall()}
    definitions = {
        "archived": "INTEGER NOT NULL DEFAULT 0 CHECK (archived IN (0,1))",
        "archived_at": "TEXT",
        "archive_reason": "TEXT",
    }
    for name, definition in definitions.items():
        if name not in columns:
            connection.execute(f"ALTER TABLE cleaning_run ADD COLUMN {name} {definition}")


def main() -> None:
    if not DB_PATH.exists():
        raise SystemExit(f"数据库不存在：{DB_PATH}")

    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    try:
        ensure_columns(connection)
        targets = connection.execute(
            """
            SELECT cleaning_run_id,rule_key,status,stage,published_count
            FROM cleaning_run
            WHERE COALESCE(archived,0)=0
              AND stage='replayed'
              AND status IN ('pending_approval','draft')
              AND COALESCE(published_count,0)=0
            ORDER BY created_at,cleaning_run_id
            """
        ).fetchall()
        if not targets:
            print(json.dumps({"archived": 0, "status": "already_clean"}, ensure_ascii=False))
            return

        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup_dir = BACKUP_ROOT / f"cleaning-task-archive-pre-{stamp}"
        backup_dir.mkdir(parents=True, exist_ok=False)
        backup_path = backup_dir / DB_PATH.name
        backup_connection = sqlite3.connect(backup_path)
        connection.backup(backup_connection)
        backup_connection.close()

        now = utc_now()
        reason = "清理历史回放待审批积压；保留审计证据，不进入当前工作列表"
        ids = [row["cleaning_run_id"] for row in targets]
        connection.executemany(
            """
            UPDATE cleaning_run
            SET archived=1,archived_at=?,archive_reason=?,updated_at=?
            WHERE cleaning_run_id=? AND COALESCE(archived,0)=0
            """,
            [(now, reason, now, task_id) for task_id in ids],
        )
        rule_keys = sorted({row["rule_key"] for row in targets})
        connection.executemany(
            "UPDATE cleaning_rule_registry SET enabled=0,updated_at=? WHERE rule_key=?",
            [(now, rule_key) for rule_key in rule_keys],
        )
        payload = {
            "archived_count": len(ids),
            "rule_count": len(rule_keys),
            "task_ids_sha256": hashlib.sha256("\n".join(ids).encode("utf-8")).hexdigest(),
            "backup_path": str(backup_path),
            "reason": reason,
            "source_write": False,
            "formal_publication": False,
        }
        connection.execute(
            "INSERT INTO audit_event(entity_type,entity_id,event_type,actor,payload_json,event_at) VALUES (?,?,?,?,?,?)",
            ("cleaning_backlog", f"archive-{stamp}", "cleaning_tasks_archived", "local-user", json.dumps(payload, ensure_ascii=False), now),
        )
        connection.commit()
        (backup_dir / "archive_manifest.json").write_text(json.dumps({**payload, "archived_at": now}, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(payload, ensure_ascii=False))
    finally:
        connection.close()


if __name__ == "__main__":
    main()
