"""Import replay-passed semantic rule previews into the local cleaning queue.

The import is deliberately local and idempotent:
* DM8/MaxiEAM is never opened or written;
* a consistent SQLite backup is made before the first write;
* only replay-passed, non-blocked queue entries are imported;
* candidates remain pending and formal publication stays false;
* the artifact source identity key is preserved as SITEID + ASSETNUM.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import shutil
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from safe_convert import to_int


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_text(value: str) -> str:
    return sha256_bytes(value.encode("utf-8"))


def queue_content_hash(queue: list[dict[str, Any]], blocked: list[dict[str, Any]]) -> str:
    """Stable idempotency hash over the semantic queue content only.

    The queue file also embeds producer metadata (``run_id``, ``created_at``,
    and input file paths) that legitimately changes on every rebuild. Hashing
    the raw file bytes would therefore mint a new batch id each time the same
    queue is rebuilt, silently importing a second set of devices, candidates,
    replays, and approval rows. Hash only the replay-passed rules and the
    blocked list, which is the semantic boundary of the import.
    """
    canonical = {"queue": queue, "blocked": blocked}
    return sha256_text(json.dumps(canonical, ensure_ascii=False, sort_keys=True))


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def clean(value: Any) -> str:
    return "" if value is None else str(value)


def key_of(row: dict[str, str]) -> tuple[str, str, str]:
    return (
        clean(row.get("source_schema") or row.get("sourceSchema")).strip(),
        clean(row.get("site_id") or row.get("siteId")).strip(),
        clean(row.get("asset_number") or row.get("assetNumber")).strip(),
    )


def resolve_preview_file(value: str, root: Path) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return root / "semantic_agent_rule_previews_v1" / path


def make_backup(db_path: Path, backup_path: Path) -> None:
    if backup_path.exists():
        raise RuntimeError(f"backup target already exists: {backup_path}")
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=60)
    target = sqlite3.connect(str(backup_path), timeout=60)
    try:
        source.execute("PRAGMA query_only=ON")
        source.backup(target, pages=2000, sleep=0.1)
        target.commit()
    finally:
        target.close()
        source.close()
    if backup_path.stat().st_size <= 0:
        raise RuntimeError("SQLite backup is empty")


def load_import_rows(queue: list[dict[str, Any]], result_root: Path) -> tuple[list[dict[str, Any]], dict[str, int], dict[str, dict[str, Any]]]:
    full_preview = result_root / "source_scoped_semantic_full_preview_v1" / "hd_saas_full_semantic_preview.csv"
    full_preview_xny = result_root / "source_scoped_semantic_full_preview_v1" / "xny_saas_full_semantic_preview.csv"
    context_by_key: dict[tuple[str, str, str], dict[str, str]] = {}
    for path in (full_preview, full_preview_xny):
        for row in read_rows(path):
            key = key_of(row)
            if any(not part for part in key):
                raise RuntimeError(f"full preview contains incomplete identity: {key}")
            if key in context_by_key:
                raise RuntimeError(f"full preview has duplicate identity: {key}")
            context_by_key[key] = row

    imported: list[dict[str, Any]] = []
    rule_counts: dict[str, int] = Counter()
    rule_metadata: dict[str, dict[str, Any]] = {}
    seen_keys: set[tuple[str, str, str]] = set()
    for item in queue:
        if item.get("replayStatus") != "passed":
            continue
        queue_id = clean(item.get("queueId"))
        cluster_id = clean(item.get("clusterId")) or "semantic-safe-whitespace-v1"
        files = [resolve_preview_file(clean(item.get("previewFile")), result_root)] if item.get("previewFile") else [Path(value) for value in item.get("previewFiles", [])]
        rows_for_rule: list[dict[str, str]] = []
        for path in files:
            if not path.exists():
                raise RuntimeError(f"preview file missing: {path}")
            rows_for_rule.extend(read_rows(path))
        if not rows_for_rule:
            raise RuntimeError(f"replay-passed rule has no rows: {queue_id}")

        source_scopes = item.get("sourceSchema")
        if isinstance(source_scopes, list):
            source_scope_label = "/".join(clean(value) for value in source_scopes)
        else:
            source_scope_label = clean(source_scopes)
        rule_metadata[queue_id] = {
            "queue_id": queue_id,
            "cluster_id": cluster_id,
            "source_schema": source_scope_label,
            "from": clean(item.get("from")),
            "to": clean(item.get("to")),
            "preview_count_declared": to_int(item.get("previewCount"), 0),
            "preview_files": [str(path) for path in files],
            "replay_status": item.get("replayStatus"),
        }
        for row in rows_for_rule:
            schema, site, asset = key_of(row)
            key = (schema, site, asset)
            if any(not part for part in key):
                raise RuntimeError(f"preview contains incomplete identity in {queue_id}: {key}")
            if key in seen_keys:
                raise RuntimeError(f"same source identity appears in multiple approved rules: {key}")
            seen_keys.add(key)
            context = context_by_key.get(key)
            if context is None:
                raise RuntimeError(f"preview identity missing from full context snapshot: {key}")
            original = clean(row.get("original_description"))
            if not original:
                original = clean(context.get("original_description"))
            proposed = clean(row.get("rule_proposed_description")) or clean(row.get("proposed_normalized_description"))
            if not proposed:
                proposed = clean(context.get("proposed_normalized_description"))
            if original != clean(context.get("original_description")):
                raise RuntimeError(f"original description mismatch for {key}")
            source_row_hash = clean(row.get("source_row_hash")) or clean(context.get("source_row_hash"))
            if not source_row_hash:
                raise RuntimeError(f"source row hash missing for {key}")
            source_asset_id = clean(row.get("source_asset_id")) or clean(context.get("source_asset_id")) or asset
            imported.append(
                {
                    "queue_id": queue_id,
                    "cluster_id": cluster_id,
                    "source_schema": schema,
                    "site_id": site,
                    "asset_number": asset,
                    "source_asset_id": source_asset_id,
                    "original_description": original,
                    "proposed_description": proposed,
                    "source_row_hash": source_row_hash,
                    "location_code": clean(context.get("location_code")),
                    "location_parent": clean(context.get("parent_asset_number")),
                    "classification_description": clean(context.get("classification_description")),
                    "class_structure_description": clean(context.get("class_structure_description")),
                    "context_hash": sha256_text(json.dumps({
                        "location_code": clean(context.get("location_code")),
                        "parent_asset_number": clean(context.get("parent_asset_number")),
                        "classification_description": clean(context.get("classification_description")),
                        "class_structure_description": clean(context.get("class_structure_description")),
                    }, ensure_ascii=False, sort_keys=True)),
                }
            )
        rule_counts[queue_id] = len(rows_for_rule)
    return imported, dict(rule_counts), rule_metadata


def open_rw(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=120)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=120000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def import_queue(args: argparse.Namespace) -> dict[str, Any]:
    queue_payload = json.loads(args.queue.read_text(encoding="utf-8"))
    queue = queue_payload.get("queue", [])
    blocked = queue_payload.get("blocked", [])
    imported, rule_counts, rule_metadata = load_import_rows(queue, args.result_root)
    # The queue's semantic content (replay-passed rules + blocked list), not
    # the producer's wall-clock run id / created_at, is the idempotency
    # boundary. Rebuilding the same queue must return the existing local
    # batch instead of inserting a second set of devices, candidates,
    # replays, and approval rows.
    queue_hash = queue_content_hash(queue, blocked)
    batch_id = f"semantic-rule-approval-{queue_hash[:40]}"
    source_snapshot_id = f"semantic-source-{args.result_root.name}-{queue_hash[:24]}"
    run_id = f"{batch_id}-run"
    rule_version = "semantic-agent-rule-queue-v1"
    validator_version = "semantic-agent-rule-validator-v2"
    now = utc_now()

    conn = open_rw(args.db)
    try:
        existing = conn.execute("SELECT status,candidate_count,formal_publication FROM batch_run WHERE batch_id=?", (batch_id,)).fetchone()
        if existing:
            return {"status": "already_imported", "batch_id": batch_id, "existing": dict(existing)}
        if args.apply:
            make_backup(args.db, args.backup)
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO source_snapshot(source_snapshot_id,connection_id,source_table,source_filter,source_row_count,distinct_identity_count,snapshot_hash,snapshot_path,source_write,captured_at,status) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (source_snapshot_id, None, "LOCAL_IDENTITY_RESULT.semantic_preview", "replay-passed rules only; blocked and ambiguous rules excluded", len(imported), len(imported), queue_hash, str(args.queue), 0, now, "verified"),
        )
        conn.execute(
            "INSERT INTO batch_run(batch_id,run_id,source_snapshot_id,batch_type,rule_version,validator_version,input_count,candidate_count,needs_review_count,blocked_count,failed_count,approved_count,published_count,status,config_json,started_at,finished_at,source_write,formal_publication) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (batch_id, run_id, source_snapshot_id, "semantic_rule_approval_queue", rule_version, validator_version, len(imported), len(imported), len(imported), 0, 0, 0, 0, "completed", json.dumps({"queue_file": str(args.queue), "queue_sha256": queue_hash, "blocked_count": len(blocked), "rule_count": len(rule_counts), "source_write": False, "formal_publication": False}, ensure_ascii=False), now, now, 0, 0),
        )

        device_rows: list[tuple[Any, ...]] = []
        candidate_rows: list[tuple[Any, ...]] = []
        identity_to_candidate: dict[str, tuple[str, str, str, str]] = {}
        for item in imported:
            identity_text = "|".join((item["source_schema"], item["site_id"], item["asset_number"], item["queue_id"]))
            digest = sha256_text(identity_text + "|" + item["source_row_hash"])
            device_id_text = f"{source_snapshot_id}|{item['source_schema']}|{item['site_id']}|{item['asset_number']}"
            analytics_key = f"{source_snapshot_id}|{item['source_schema']}|{item['site_id']}|{item['asset_number']}"
            device_rows.append((source_snapshot_id, item["source_schema"], item["source_asset_id"], item["site_id"], item["asset_number"], item["source_row_hash"], item["original_description"], item["location_code"], None, item["location_parent"], item["classification_description"], item["class_structure_description"], item["context_hash"], analytics_key, now))
            identity_to_candidate[device_id_text] = (f"candidate-semantic-{digest[:40]}", item["queue_id"], item["cluster_id"], digest)

        conn.executemany(
            "INSERT INTO device_identity(source_snapshot_id,source_schema,source_asset_id,site_id,asset_number,source_row_hash,original_description,location_code,location_description,location_parent,classification_description,class_structure_description,context_hash,analytics_row_key,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            device_rows,
        )
        device_lookup = {
            (row["source_schema"], row["site_id"], row["asset_number"]): int(row["device_id"])
            for row in conn.execute("SELECT device_id,source_schema,site_id,asset_number FROM device_identity WHERE source_snapshot_id=?", (source_snapshot_id,))
        }

        for item in imported:
            key = (item["source_schema"], item["site_id"], item["asset_number"])
            device_id = device_lookup[key]
            identity_text = "|".join((item["source_schema"], item["site_id"], item["asset_number"], item["queue_id"]))
            digest = sha256_text(identity_text + "|" + item["source_row_hash"])
            candidate_id = f"candidate-semantic-{digest[:40]}"
            rule_key = f"semantic.queue.{item['queue_id']}.{queue_hash[:16]}"
            reason_codes = ["replay_passed", "source_identity_verified", "human_approval_required"]
            candidate_rows.append((candidate_id, batch_id, device_id, item["original_description"], item["proposed_description"], "normalize", "high", "candidate", json.dumps(reason_codes, ensure_ascii=False), "source_preview_and_replay", json.dumps([item["cluster_id"]], ensure_ascii=False), sha256_text(json.dumps({"identity": key, "original": item["original_description"], "proposed": item["proposed_description"], "rule_key": rule_key}, ensure_ascii=False, sort_keys=True)), rule_version, validator_version, "pending", "unpublished", now))
            item["candidate_id"] = candidate_id
            item["rule_key"] = rule_key

        conn.executemany(
            "INSERT INTO semantic_candidate(candidate_id,batch_id,device_id,original_description,candidate_description,semantic_action,confidence,validator_status,reason_codes_json,evidence_level,applied_rule_ids_json,candidate_hash,rule_version,validator_version,review_state,publication_state,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            candidate_rows,
        )

        for queue_id, count_rows in rule_counts.items():
            meta = rule_metadata[queue_id]
            replay_id = f"replay-semantic-{queue_id}-{queue_hash[:16]}"
            rule_key = f"semantic.queue.{queue_id}.{queue_hash[:16]}"
            cleaning_run_id = f"cleaning-run-{replay_id}"
            metadata_json = json.dumps({**meta, "queue_hash": queue_hash, "source_write": False, "formal_publication": False}, ensure_ascii=False)
            label_from = meta["from"] or "空格变体"
            label_to = meta["to"] or "单一规范空格"
            rule_label = f"{meta['source_schema']}：{label_from} → {label_to}"
            cleaning_type = "whitespace" if queue_id == "rule-approval-safe-whitespace-trim-collapse-v1" else "unicode_format"
            preview_paths = json.dumps(meta["preview_files"], ensure_ascii=False)
            conn.execute(
                "INSERT INTO replay_run(replay_id,rule_version,validator_version,evaluation_count,pass_count,fail_count,status,started_at,finished_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (replay_id, rule_version, validator_version, count_rows, count_rows, 0, "passed", now, now),
            )
            conn.execute(
                "INSERT INTO cleaning_rule_registry(rule_key,cleaning_type,rule_label,action_label,is_cleaning,replay_id,rule_version,enabled,metadata_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                # Replay success is evidence for review; it is not approval.
                # A rule must remain disabled until the explicit rule approval
                # and enable operation is completed.
                (rule_key, cleaning_type, rule_label, "待人工确认后启用", 1, replay_id, rule_version, 0, metadata_json, now, now),
            )
            conn.execute(
                "INSERT INTO cleaning_run(cleaning_run_id,rule_key,replay_id,batch_id,source_type,status,candidate_count,pending_count,approved_count,published_count,preview_path,sample_path,created_at,updated_at,stage,preview_id,preview_sha256,source_write,formal_publication,preview_rows,replay_rows,last_error) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (cleaning_run_id, rule_key, replay_id, batch_id, "semantic_rule_queue", "pending_approval", count_rows, count_rows, 0, 0, preview_paths, str(args.result_root / "source_scoped_semantic_review_v1" / "representative_review_set.csv"), now, now, "replayed", f"artifact-preview-{queue_id}", queue_hash, 0, 0, count_rows, count_rows, None),
            )
            rule_candidates = [item for item in imported if item["queue_id"] == queue_id]
            conn.executemany(
                "INSERT INTO formal_approval_queue(queue_id,candidate_id,cluster_id,replay_id,proposed_decision,proposed_description,status,note,created_at,updated_at,source_write,formal_publication) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                [(f"formal-{sha256_text(item['candidate_id'])[:40]}", item["candidate_id"], item["cluster_id"], replay_id, "approved", item["proposed_description"], "pending", "AI/规则草案已回放通过，等待人工确认", now, now, 0, 0) for item in rule_candidates],
            )

        conn.execute(
            "INSERT INTO audit_event(entity_type,entity_id,event_type,actor,payload_json,event_at) VALUES (?,?,?,?,?,?)",
            ("semantic_rule_queue", batch_id, "semantic_rule_queue_imported", "local-user", json.dumps({"batch_id": batch_id, "source_snapshot_id": source_snapshot_id, "rule_count": len(rule_counts), "candidate_count": len(imported), "blocked_count": len(blocked), "source_write": False, "formal_publication": False}, ensure_ascii=False), now),
        )
        conn.commit()

        checks = {
            "batch_candidates": int(conn.execute("SELECT count(*) FROM semantic_candidate WHERE batch_id=?", (batch_id,)).fetchone()[0]),
            "batch_queue": int(conn.execute("SELECT count(*) FROM formal_approval_queue q JOIN semantic_candidate c ON c.candidate_id=q.candidate_id WHERE c.batch_id=?", (batch_id,)).fetchone()[0]),
            "batch_rules": int(conn.execute("SELECT count(*) FROM cleaning_run WHERE batch_id=?", (batch_id,)).fetchone()[0]),
            "pending_queue": int(conn.execute("SELECT count(*) FROM formal_approval_queue q JOIN semantic_candidate c ON c.candidate_id=q.candidate_id WHERE c.batch_id=? AND q.status='pending'", (batch_id,)).fetchone()[0]),
            "published_candidates": int(conn.execute("SELECT count(*) FROM semantic_candidate WHERE batch_id=? AND publication_state='published'", (batch_id,)).fetchone()[0]),
            "formal_publication_flags": int(conn.execute("SELECT count(*) FROM cleaning_run WHERE batch_id=? AND formal_publication=1", (batch_id,)).fetchone()[0]),
        }
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()

    report = {
        "status": "imported",
        "batch_id": batch_id,
        "source_snapshot_id": source_snapshot_id,
        "rule_count": len(rule_counts),
        "candidate_count": len(imported),
        "blocked_count": len(blocked),
        "queue_sha256": queue_hash,
        "checks": checks,
        "backup_path": str(args.backup),
        "source_write": False,
        "formal_publication": False,
        "created_at": now,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--queue", type=Path, required=True)
    parser.add_argument("--result-root", type=Path, required=True)
    parser.add_argument("--backup", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--plan", action="store_true")
    args = parser.parse_args()
    if args.plan:
        payload = json.loads(args.queue.read_text(encoding="utf-8"))
        rows, counts, _ = load_import_rows(payload.get("queue", []), args.result_root)
        print(json.dumps({"mode": "plan", "rule_count": len(counts), "candidate_count": len(rows), "blocked_count": len(payload.get("blocked", [])), "rule_counts": counts}, ensure_ascii=False, indent=2))
        return
    if not args.apply:
        raise SystemExit("refusing to write without --apply")
    report = import_queue(args)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
