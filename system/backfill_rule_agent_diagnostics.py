"""Backfill stable diagnostics for historical rule-agent failures.

This only enriches local workflow audit columns.  It does not retry model calls,
change proposals, approve rules, or write any source system.
"""
from __future__ import annotations

import json
import pathlib
import sqlite3

ROOT = pathlib.Path(__file__).resolve().parent
DB = ROOT / "data" / "semantic_workflow.sqlite3"


def classify(message: str) -> tuple[str, bool]:
    value = (message or "").lower()
    if "403" in value or "401" in value:
        return "provider_http_auth", False
    if "timeout" in value:
        return "provider_timeout", True
    if "connection" in value or "httperror" in value:
        return "provider_connection", True
    if "json" in value:
        return "invalid_json", True
    if "配置" in (message or "") or "configured" in value:
        return "agent_not_configured", False
    return "historical_agent_error", False


def main() -> None:
    db = sqlite3.connect(str(DB))
    db.row_factory = sqlite3.Row
    try:
        columns = {row[1] for row in db.execute("PRAGMA table_info(rule_agent_run)")}
        required = {"error_code", "retryable", "attempt_count", "fallback_used"}
        missing = required - columns
        if missing:
            raise RuntimeError(f"先运行后端初始化以补齐字段: {sorted(missing)}")
        rows = db.execute(
            "SELECT run_id,error_message,error_code FROM rule_agent_run WHERE status='failed' ORDER BY created_at"
        ).fetchall()
        updated = 0
        for row in rows:
            if row["error_code"]:
                continue
            code, retryable = classify(str(row["error_message"] or ""))
            db.execute(
                "UPDATE rule_agent_run SET error_code=?,retryable=?,attempt_count=CASE WHEN attempt_count=0 THEN 1 ELSE attempt_count END WHERE run_id=?",
                (code, int(retryable), row["run_id"]),
            )
            updated += 1
        db.commit()
        counts = [dict(row) for row in db.execute(
            "SELECT coalesce(error_code,'unknown') AS error_code,retryable,count(*) AS count FROM rule_agent_run WHERE status='failed' GROUP BY error_code,retryable ORDER BY error_code"
        )]
        print(json.dumps({
            "status": "passed",
            "updated": updated,
            "failed_run_diagnostics": counts,
            "source_write": False,
            "formal_publication": False,
        }, ensure_ascii=False, indent=2))
    finally:
        db.close()


if __name__ == "__main__":
    from pipeline.legacy import run_legacy_main

    raise SystemExit(
        run_legacy_main(
            pipeline_id="backfill-rule-agent-diagnostics",
            pipeline_version="v1",
            root=ROOT,
            legacy_main=main,
        )
    )
