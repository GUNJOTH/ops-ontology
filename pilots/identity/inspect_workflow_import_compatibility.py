"""Read-only compatibility audit for importing the latest semantic rule queue.

This script never opens the workflow database in write mode.  It compares the
identity keys in the audited preview CSVs with the existing local harness
candidate layer so registration can be done without guessing an identity.
"""
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path


def ro_connect(path: Path) -> sqlite3.Connection:
    uri = f"file:{path.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def table_names(conn: sqlite3.Connection) -> list[str]:
    return [
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
    ]


def columns(conn: sqlite3.Connection, name: str) -> list[str]:
    return [row[1] for row in conn.execute(f"PRAGMA table_info({quote_ident(name)})").fetchall()]


def count(conn: sqlite3.Connection, name: str) -> int | None:
    if name not in table_names(conn):
        return None
    return int(conn.execute(f"SELECT count(*) FROM {quote_ident(name)}").fetchone()[0])


def load_queue(queue_path: Path) -> tuple[list[dict], list[dict]]:
    payload = json.loads(queue_path.read_text(encoding="utf-8"))
    return payload.get("queue", []), payload.get("blocked", [])


def preview_files(queue_item: dict, result_root: Path) -> list[Path]:
    files: list[Path] = []
    if queue_item.get("previewFile"):
        value = Path(queue_item["previewFile"])
        files.append(value if value.is_absolute() else result_root / "semantic_agent_rule_previews_v1" / value)
    for value in queue_item.get("previewFiles", []):
        path = Path(value)
        files.append(path if path.is_absolute() else result_root / value)
    return files


def load_artifact_rows(queue: list[dict], result_root: Path) -> tuple[list[dict], Counter, dict[str, int]]:
    rows: list[dict] = []
    rule_counts: Counter = Counter()
    missing_files: dict[str, int] = {}
    for item in queue:
        files = preview_files(item, result_root)
        if not files:
            missing_files[item.get("queueId", "unknown")] = 0
            continue
        item_count = 0
        for path in files:
            if not path.exists():
                missing_files[str(path)] = 0
                continue
            with path.open("r", encoding="utf-8-sig", newline="") as handle:
                for row in csv.DictReader(handle):
                    source_schema = (row.get("source_schema") or row.get("sourceSchema") or "").strip()
                    site_id = (row.get("site_id") or row.get("siteId") or "").strip()
                    asset_number = (row.get("asset_number") or row.get("assetNumber") or "").strip()
                    if not source_schema or not site_id or not asset_number:
                        continue
                    rows.append(
                        {
                            "queue_id": item.get("queueId"),
                            "cluster_id": item.get("clusterId") or item.get("ruleKey"),
                            "source_schema": source_schema,
                            "site_id": site_id,
                            "asset_number": asset_number,
                            "original_description": row.get("original_description", ""),
                            "proposed_description": row.get("rule_proposed_description", ""),
                            "source_row_hash": row.get("source_row_hash", ""),
                        }
                    )
                    item_count += 1
        rule_counts[item.get("queueId", "unknown")] += item_count
    return rows, rule_counts, missing_files


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--queue", type=Path, required=True)
    parser.add_argument("--result-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    conn = ro_connect(args.db)
    try:
        tables = table_names(conn)
        queue, blocked = load_queue(args.queue)
        artifact_rows, rule_counts, missing_files = load_artifact_rows(queue, args.result_root)

        schema = {name: columns(conn, name) for name in ("source_connection", "batch_run", "source_snapshot", "device_identity", "semantic_candidate", "replay_run", "formal_approval_queue", "cleaning_rule_registry", "cleaning_run", "audit_event") if name in tables}
        counts = {name: count(conn, name) for name in schema}
        create_sql = {
            name: conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (name,)
            ).fetchone()[0]
            for name in schema
        }
        latest_rows = {}
        for name in ("source_connection", "batch_run", "source_snapshot", "replay_run"):
            if name in tables:
                latest_rows[name] = [
                    dict(row)
                    for row in conn.execute(f"SELECT * FROM {quote_ident(name)} ORDER BY rowid DESC LIMIT 3").fetchall()
                ]

        identity_columns = set(schema.get("device_identity", []))
        candidate_columns = set(schema.get("semantic_candidate", []))
        if not {"site_id", "asset_number"}.issubset(identity_columns):
            raise RuntimeError("device_identity does not expose site_id and asset_number")
        if not {"device_id", "candidate_id"}.issubset(identity_columns | candidate_columns):
            raise RuntimeError("workflow identity/candidate key columns are incomplete")

        existing_identity_sql = "SELECT device_id,site_id,asset_number FROM device_identity"
        identity_map = {
            (str(row["site_id"]), str(row["asset_number"])): str(row["device_id"])
            for row in conn.execute(existing_identity_sql)
        }
        candidate_by_device = {
            str(row["device_id"]): str(row["candidate_id"])
            for row in conn.execute("SELECT device_id,candidate_id FROM semantic_candidate")
        }

        matched = 0
        candidate_matched = 0
        unmatched: Counter = Counter()
        candidate_duplicates: Counter = Counter()
        identity_seen: Counter = Counter()
        identity_proposals: defaultdict[tuple[str, str], set[str]] = defaultdict(set)
        rule_identity_counts: Counter = Counter()
        for row in artifact_rows:
            key = (row["site_id"], row["asset_number"])
            identity_seen[key] += 1
            identity_proposals[key].add(row["proposed_description"])
            rule_identity_counts[row["queue_id"]] += 1
            device_id = identity_map.get(key)
            if device_id is None:
                unmatched[row["source_schema"]] += 1
                continue
            matched += 1
            candidate_id = candidate_by_device.get(device_id)
            if candidate_id is None:
                unmatched[f"candidate_missing:{row['source_schema']}"] += 1
            else:
                candidate_matched += 1
                candidate_duplicates[candidate_id] += 1

        duplicate_identity_rows = sum(value - 1 for value in identity_seen.values() if value > 1)
        conflicting_identity_count = sum(1 for proposals in identity_proposals.values() if len(proposals) > 1)
        duplicate_candidate_rows = sum(value - 1 for value in candidate_duplicates.values() if value > 1)

        result = {
            "read_only": True,
            "db": str(args.db),
            "queue_file": str(args.queue),
            "table_names": tables,
            "schema": schema,
            "create_sql": create_sql,
            "latest_rows": latest_rows,
            "counts": counts,
            "queue_count": len(queue),
            "blocked_count": len(blocked),
            "artifact_row_count": len(artifact_rows),
            "missing_preview_files": missing_files,
            "identity_key": ["site_id", "asset_number"],
            "identity_matched_rows": matched,
            "candidate_matched_rows": candidate_matched,
            "unmatched_by_reason": dict(unmatched),
            "duplicate_identity_rows": duplicate_identity_rows,
            "conflicting_identity_count": conflicting_identity_count,
            "duplicate_candidate_rows": duplicate_candidate_rows,
            "rule_row_counts": dict(rule_counts),
            "rule_identity_counts": dict(rule_identity_counts),
            "decision": "safe_to_register" if not unmatched and not conflicting_identity_count else "do_not_write",
        }
    finally:
        conn.close()

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: result[key] for key in ("queue_count", "artifact_row_count", "identity_matched_rows", "candidate_matched_rows", "unmatched_by_reason", "duplicate_identity_rows", "conflicting_identity_count", "duplicate_candidate_rows", "decision")}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
