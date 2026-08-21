"""Create the local audit ledger for semantic action previews.

The ledger records execution plans and gate results only.  It is deliberately
not an executor for source writes or formal publication.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sqlite3
from datetime import datetime, timezone


ROOT = pathlib.Path(__file__).resolve().parent
DEFAULT_TARGET = ROOT / "data" / "unified_semantics.sqlite3"


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def build(target_path: pathlib.Path) -> dict[str, object]:
    db = sqlite3.connect(str(target_path), timeout=30)
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS semantic_action_run (
          run_id TEXT PRIMARY KEY,
          idempotency_key TEXT NOT NULL UNIQUE,
          asset_id TEXT NOT NULL,
          asset_version_id TEXT NOT NULL,
          action_id TEXT NOT NULL,
          mode TEXT NOT NULL CHECK (mode IN ('preview','replay','approval','publication')),
          status TEXT NOT NULL CHECK (status IN ('planned','ready','needs_review','blocked','completed','failed')),
          target_scope TEXT NOT NULL,
          target_count INTEGER NOT NULL DEFAULT 0,
          gate_summary_json TEXT NOT NULL,
          action_plan_json TEXT NOT NULL,
          source_write INTEGER NOT NULL CHECK (source_write=0),
          formal_publication INTEGER NOT NULL CHECK (formal_publication=0),
          created_at TEXT NOT NULL,
          finished_at TEXT
        );
        CREATE INDEX IF NOT EXISTS ix_semantic_action_run_asset ON semantic_action_run(asset_id,created_at);
        CREATE INDEX IF NOT EXISTS ix_semantic_action_run_status ON semantic_action_run(status,created_at);
        CREATE TABLE IF NOT EXISTS semantic_execution_adapter (
          adapter_id TEXT PRIMARY KEY,
          adapter_version TEXT NOT NULL,
          action_type TEXT NOT NULL,
          target_system TEXT NOT NULL,
          endpoint_name TEXT NOT NULL,
          mode TEXT NOT NULL CHECK(mode IN ('local_preview','approval_required','external_enabled')),
          status TEXT NOT NULL CHECK(status IN ('active','disabled','needs_review')),
          source_write INTEGER NOT NULL CHECK(source_write=0),
          formal_publication INTEGER NOT NULL CHECK(formal_publication=0),
          config_json TEXT NOT NULL DEFAULT '{}',
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          UNIQUE(action_type,target_system,adapter_version)
        );
        CREATE TABLE IF NOT EXISTS semantic_execution_ledger (
          execution_id TEXT PRIMARY KEY,
          action_run_id TEXT,
          action_plan_id TEXT,
          approval_receipt TEXT,
          adapter_id TEXT NOT NULL REFERENCES semantic_execution_adapter(adapter_id),
          idempotency_key TEXT NOT NULL UNIQUE,
          status TEXT NOT NULL CHECK(status IN ('planned','blocked','submitted','succeeded','failed','skipped')),
          external_execution_ref TEXT,
          request_json TEXT NOT NULL DEFAULT '{}',
          response_json TEXT NOT NULL DEFAULT '{}',
          error_code TEXT,
          error_message TEXT,
          source_write INTEGER NOT NULL CHECK(source_write=0),
          formal_publication INTEGER NOT NULL CHECK(formal_publication=0),
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          completed_at TEXT
        );
        CREATE INDEX IF NOT EXISTS ix_semantic_execution_ledger_status
          ON semantic_execution_ledger(status,updated_at);
        CREATE INDEX IF NOT EXISTS ix_semantic_execution_ledger_plan
          ON semantic_execution_ledger(action_plan_id,created_at);
        """
    )
    created = now()
    db.execute(
        """INSERT INTO semantic_execution_adapter(
          adapter_id,adapter_version,action_type,target_system,endpoint_name,mode,
          status,source_write,formal_publication,config_json,created_at,updated_at
        ) VALUES ('local-preview-adapter','execution-adapter-v1','*','LOCAL_SEMANTIC','preview-only','local_preview','active',0,0,?,?,?)
        ON CONFLICT(adapter_id) DO UPDATE SET adapter_version=excluded.adapter_version,
          mode=excluded.mode,status=excluded.status,config_json=excluded.config_json,updated_at=excluded.updated_at""",
        (json.dumps({"source_write": False, "formal_publication": False, "note": "仅生成执行台账，不调用外部系统"}, ensure_ascii=False), created, created),
    )
    db.commit()
    action_run_count = int(db.execute("SELECT count(*) FROM semantic_action_run").fetchone()[0])
    adapter_count = int(db.execute("SELECT count(*) FROM semantic_execution_adapter WHERE status='active'").fetchone()[0])
    ledger_count = int(db.execute("SELECT count(*) FROM semantic_execution_ledger").fetchone()[0])
    unsafe_count = int(db.execute("SELECT count(*) FROM semantic_execution_ledger WHERE source_write<>0 OR formal_publication<>0").fetchone()[0])
    db.close()
    return {
        "status": "ready" if unsafe_count == 0 else "blocked",
        "actionRunCount": action_run_count,
        "activeAdapterCount": adapter_count,
        "ledgerCount": ledger_count,
        "unsafeLedgerCount": unsafe_count,
        "sourceWrite": False,
        "formalPublication": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Create the safe semantic action preview ledger")
    parser.add_argument("--target-db", type=pathlib.Path, default=DEFAULT_TARGET)
    args = parser.parse_args()
    result = build(args.target_db.resolve())
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
