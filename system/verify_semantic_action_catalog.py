"""Verify the first-class business Action catalog and execution-state contract.

The Action catalog is a local semantic asset.  It must describe the business
meaning, allowed-when conditions, required input facts, permission scopes,
adapter mappings, effects and the requested -> executing -> succeeded/failed
execution state machine.  All entries must remain source-write safe.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sqlite3

ROOT = pathlib.Path(__file__).resolve().parent
DEFAULT_TARGET = ROOT / "data" / "unified_semantics.sqlite3"

REQUIRED_ACTION_IDS = {
    "ACTION_CREATE_DEFECT",
    "ACTION_CREATE_WORK_ORDER",
    "ACTION_STOP_PUMP",
    "ACTION_REQUEST_HUMAN_APPROVAL",
}

REQUIRED_EXECUTION_STATES = {"requested", "executing", "succeeded", "failed"}


def verify(target_path: pathlib.Path) -> dict[str, object]:
    db = sqlite3.connect(f"file:{target_path.resolve()}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if "semantic_action_definition" not in tables:
            return {
                "status": "FAIL",
                "checks": {"catalog_table_exists": False},
                "failures": ["SEMANTIC_ACTION_CATALOG_MISSING"],
                "source_write": False,
                "formal_publication": False,
            }
        rows = db.execute("SELECT * FROM semantic_action_definition ORDER BY action_id").fetchall()
        found_ids = {row["action_id"] for row in rows}
        failures: list[str] = []
        if not REQUIRED_ACTION_IDS.issubset(found_ids):
            failures.append("SEMANTIC_ACTION_REQUIRED_IDS_MISSING:" + ",".join(sorted(REQUIRED_ACTION_IDS - found_ids)))
        for row in rows:
            action_id = row["action_id"]
            for column in ("action_name", "business_meaning", "target_type"):
                if not str(row[column] or "").strip():
                    failures.append(f"SEMANTIC_ACTION_FIELD_EMPTY:{action_id}:{column}")
            for column, container in (
                ("allowed_when_json", dict),
                ("required_facts_json", list),
                ("permission_scope_json", dict),
                ("adapter_mappings_json", list),
                ("effects_json", dict),
                ("execution_states_json", list),
            ):
                try:
                    parsed = json.loads(row[column] or "")
                    if not isinstance(parsed, container) or not parsed:
                        failures.append(f"SEMANTIC_ACTION_JSON_INVALID:{action_id}:{column}")
                except json.JSONDecodeError:
                    failures.append(f"SEMANTIC_ACTION_JSON_INVALID:{action_id}:{column}")
            if int(row["source_write"]) != 0 or int(row["formal_publication"]) != 0:
                failures.append(f"SEMANTIC_ACTION_UNSAFE_FLAG:{action_id}")
            try:
                states = set(json.loads(row["execution_states_json"] or "[]"))
                if not REQUIRED_EXECUTION_STATES.issubset(states):
                    failures.append(f"SEMANTIC_ACTION_EXECUTION_STATES_INVALID:{action_id}")
            except json.JSONDecodeError:
                failures.append(f"SEMANTIC_ACTION_EXECUTION_STATES_INVALID:{action_id}")

        execution_count = 0
        unsafe_execution = 0
        if "semantic_action_execution" in tables:
            execution_count = int(db.execute("SELECT count(*) FROM semantic_action_execution").fetchone()[0])
            unsafe_execution = int(db.execute(
                "SELECT count(*) FROM semantic_action_execution WHERE source_write<>0 OR formal_publication<>0"
            ).fetchone()[0])
        checks = {
            "catalog_table_exists": True,
            "required_actions_present": REQUIRED_ACTION_IDS.issubset(found_ids),
            "all_actions_safe": not failures,
            "execution_state_table_present": "semantic_action_execution" in tables,
            "execution_state_table_safe": unsafe_execution == 0,
        }
        status = "PASS" if not failures and unsafe_execution == 0 else "FAIL"
        return {
            "status": status,
            "action_count": len(rows),
            "required_action_ids": sorted(REQUIRED_ACTION_IDS),
            "execution_record_count": execution_count,
            "unsafe_execution_count": unsafe_execution,
            "checks": checks,
            "failures": failures,
            "source_write": False,
            "formal_publication": False,
        }
    finally:
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify the local semantic Action catalog")
    parser.add_argument("--target-db", type=pathlib.Path, default=DEFAULT_TARGET)
    args = parser.parse_args()
    result = verify(args.target_db.resolve())
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result["status"] == "PASS" else 1)


if __name__ == "__main__":
    main()
