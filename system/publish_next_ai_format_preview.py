"""Publish the confirmed 66-row safe punctuation preview only."""
from __future__ import annotations

import csv
import hashlib
import json
import shutil
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parent
DATA_DIR = ROOT / "data"
DB = DATA_DIR / "semantic_workflow.sqlite3"
DUCKDB = DATA_DIR / "semantic_analytics_v155.duckdb"
DUCKDB_WAL = DATA_DIR / "semantic_analytics_v155.duckdb.wal"
PREVIEW_DIR = PROJECT_ROOT / "pilots" / "HD_SAAS" / "next_ai_format_preview"
PREVIEW_CSV = PREVIEW_DIR / "safe_punctuation_formal_preview.csv"
MANIFEST_JSON = PREVIEW_DIR / "manifest.json"
REPLAY_JSON = PREVIEW_DIR / "replay_manifest.json"
PUBLICATION_JSON = PREVIEW_DIR / "publication_manifest.json"
RULE_VERSION = "ai-confirmed-format-preview-20260812-v1"
PUBLISHER = "local-user-confirmed-ai-format-66"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def backup(connection: sqlite3.Connection, stamp: str) -> Path:
    backup_dir = ROOT / "backups" / f"ai-format-publication-pre-{stamp}"
    backup_dir.mkdir(parents=True, exist_ok=False)
    sqlite_backup = backup_dir / DB.name
    target = sqlite3.connect(str(sqlite_backup))
    connection.backup(target)
    target.close()
    files = [sqlite_backup]
    for source in (DUCKDB, DUCKDB_WAL):
        if source.exists():
            destination = backup_dir / source.name
            shutil.copy2(source, destination)
            files.append(destination)
    (backup_dir / "manifest.json").write_text(json.dumps({
        "status": "pre_publication_backup",
        "created_at_utc": utc_now(),
        "files": [{"path": str(path), "size": path.stat().st_size} for path in files],
        "source_write": False,
        "formal_publication": False,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return backup_dir


def main() -> None:
    manifest = json.loads(MANIFEST_JSON.read_text(encoding="utf-8"))
    replay = json.loads(REPLAY_JSON.read_text(encoding="utf-8"))
    if manifest.get("rule_version") != RULE_VERSION or manifest.get("formal_publication") is not False:
        raise SystemExit("Preview is not the expected unpublished preview.")
    if manifest.get("preview_rows") != 66 or sha256_file(PREVIEW_CSV) != manifest.get("preview_sha256"):
        raise SystemExit("Preview count or hash mismatch.")
    if replay.get("status") != "passed" or replay.get("evaluation_count") != 131 or replay.get("pass_count") != 131 or replay.get("fail_count") != 0:
        raise SystemExit("The 131-case replay gate is not satisfied.")
    with PREVIEW_CSV.open(encoding="utf-8-sig", newline="") as handle:
        preview_rows = list(csv.DictReader(handle))
    if len(preview_rows) != 66 or len({row["CANDIDATE_ID"] for row in preview_rows}) != 66:
        raise SystemExit("Preview must contain 66 unique rows.")

    connection = sqlite3.connect(str(DB), timeout=120)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=120000")
    connection.execute("PRAGMA foreign_keys=ON")
    candidate_ids = sorted(row["CANDIDATE_ID"] for row in preview_rows)
    marks = ",".join("?" for _ in candidate_ids)
    candidates = connection.execute(
        f"""SELECT c.candidate_id,c.batch_id,c.original_description,c.candidate_description,
        c.review_state,c.publication_state,c.validator_status,c.confidence,c.validator_version,
        d.source_snapshot_id,d.source_schema,d.site_id,d.asset_number
        FROM semantic_candidate c JOIN device_identity d ON d.device_id=c.device_id
        WHERE c.candidate_id IN ({marks})""", candidate_ids,
    ).fetchall()
    candidate_map = {row["candidate_id"]: row for row in candidates}
    if len(candidate_map) != 66:
        raise SystemExit(f"Database/preview mismatch: {len(candidate_map)} of 66")
    preview_map = {row["CANDIDATE_ID"]: row for row in preview_rows}
    identities: set[tuple[str, str, str]] = set()
    for candidate_id, candidate in candidate_map.items():
        preview = preview_map[candidate_id]
        identity = (candidate["source_schema"], candidate["site_id"], candidate["asset_number"])
        if identity in identities:
            raise SystemExit(f"Duplicate source identity: {identity}")
        identities.add(identity)
        if candidate["publication_state"] != "unpublished" or candidate["review_state"] != "pending":
            raise SystemExit(f"Candidate is no longer pending: {candidate_id}")
        if candidate["validator_status"] != "candidate" or candidate["confidence"] != "high":
            raise SystemExit(f"Candidate is not high quality: {candidate_id}")
        if candidate["original_description"] != preview["ORIGINAL_DESCRIPTION"] or candidate["candidate_description"] != preview["PROPOSED_DESCRIPTION"]:
            raise SystemExit(f"Preview mismatch: {candidate_id}")
        if connection.execute("SELECT 1 FROM published_description WHERE source_schema=? AND site_id=? AND asset_number=?", identity).fetchone():
            raise SystemExit(f"Identity already published: {identity}")
        if connection.execute("SELECT 1 FROM review_decision WHERE candidate_id=?", (candidate_id,)).fetchone():
            raise SystemExit(f"Existing review blocks publication: {candidate_id}")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_dir = backup(connection, stamp)
    now = utc_now()
    publication_run_id = f"ai-format-publication-{uuid.uuid4().hex}"
    approval_batch_id = f"ai-format-approval-{uuid.uuid4().hex}"
    try:
        connection.execute("BEGIN IMMEDIATE")
        created_reviews = 0
        inserted = 0
        for candidate_id in candidate_ids:
            candidate = candidate_map[candidate_id]
            preview = preview_map[candidate_id]
            review_id = f"review-ai-format-{candidate_id}"
            receipt = f"receipt-ai-format-{candidate_id}"
            connection.execute(
                "INSERT INTO review_decision(review_id,candidate_id,decision,reviewed_description,reason_code,review_note,reviewer,approval_receipt,reviewed_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (review_id, candidate_id, "approved", preview["PROPOSED_DESCRIPTION"], "AI_CONFIRMED_SAFE_PUNCTUATION", "用户确认发布 66 条安全标点预览；131/131 回放通过；源库只读。", PUBLISHER, receipt, now),
            )
            created_reviews += 1
            publication_hash = sha256_bytes(json.dumps({"source_schema": candidate["source_schema"], "site_id": candidate["site_id"], "asset_number": candidate["asset_number"], "source_snapshot_id": candidate["source_snapshot_id"], "final_description": preview["PROPOSED_DESCRIPTION"], "rule_version": RULE_VERSION}, ensure_ascii=False, sort_keys=True).encode("utf-8"))
            connection.execute(
                "INSERT INTO published_description(publication_id,candidate_id,review_id,source_snapshot_id,source_schema,site_id,asset_number,final_description,rule_version,validator_version,replay_id,published_by,published_at,publication_hash) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (f"publication-ai-format-{candidate_id}", candidate_id, review_id, candidate["source_snapshot_id"], candidate["source_schema"], candidate["site_id"], candidate["asset_number"], preview["PROPOSED_DESCRIPTION"], RULE_VERSION, candidate["validator_version"], replay["replay_id"], PUBLISHER, now, publication_hash),
            )
            connection.execute("UPDATE semantic_candidate SET publication_state='published',review_state='approved',applied_rule_ids_json=? WHERE candidate_id=?", (json.dumps(["format.confirmed_safe_punctuation_shape"], ensure_ascii=False), candidate_id))
            inserted += 1
        batch_ids = {candidate_map[candidate_id]["batch_id"] for candidate_id in candidate_ids}
        if len(batch_ids) != 1:
            raise SystemExit(f"Preview spans multiple batches: {batch_ids}")
        batch_id = next(iter(batch_ids))
        connection.execute("UPDATE batch_run SET published_count=(SELECT count(1) FROM published_description p JOIN semantic_candidate c ON c.candidate_id=p.candidate_id WHERE c.batch_id=?),formal_publication=1 WHERE batch_id=?", (batch_id, batch_id))
        payload = {"publication_run_id": publication_run_id, "approval_batch_id": approval_batch_id, "batch_id": batch_id, "preview_rows": 66, "inserted_publications": inserted, "created_reviews": created_reviews, "rule_version": RULE_VERSION, "replay_id": replay["replay_id"], "backup_dir": str(backup_dir), "source_write": False, "formal_publication": True}
        connection.execute("INSERT INTO audit_event(entity_type,entity_id,event_type,actor,payload_json,event_at) VALUES (?,?,?,?,?,?)", ("publication", publication_run_id, "ai_format_preview_published", PUBLISHER, json.dumps(payload, ensure_ascii=False), now))
        connection.execute("INSERT INTO audit_event(entity_type,entity_id,event_type,actor,payload_json,event_at) VALUES (?,?,?,?,?,?)", ("approval_batch", approval_batch_id, "ai_format_batch_approval_created", PUBLISHER, json.dumps({"count": 66, "replay_id": replay["replay_id"], "source_write": False}, ensure_ascii=False), now))
        connection.commit()
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()
    publication = {**payload, "published_at_utc": now, "formal_result_layer": "published_description", "status": "published"}
    PUBLICATION_JSON.write_text(json.dumps(publication, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(publication, ensure_ascii=False))


if __name__ == "__main__":
    main()
