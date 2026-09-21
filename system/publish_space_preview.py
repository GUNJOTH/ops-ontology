"""Publish the confirmed space-normalization preview into the formal layer."""
from __future__ import annotations

import csv
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from common import (
    DEFAULT_DUCKDB,
    DEFAULT_WORKFLOW,
    backup_before_publish,
    sha256_bytes,
    sha256_file,
    utc_now,
)
from pipeline.contracts import connect_local

ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parent
DB = DEFAULT_WORKFLOW
DUCKDB = DEFAULT_DUCKDB
DUCKDB_WAL = Path(str(DEFAULT_DUCKDB) + ".wal")
PREVIEW_DIR = PROJECT_ROOT / "pilots" / "HD_SAAS" / "space_rule_preview"
PREVIEW_CSV = PREVIEW_DIR / "formal_space_rewrite_preview.csv"
PREVIEW_MANIFEST = PREVIEW_DIR / "formal_preview_manifest.json"
ACTIVATION_MANIFEST = PREVIEW_DIR / "activation_manifest.json"
REPLAY_MANIFEST = PREVIEW_DIR / "replay_manifest.json"
PUBLICATION_MANIFEST = PREVIEW_DIR / "publication_manifest.json"
RULE_VERSION = "space-normalization-proposed-20260812-v1"
RULE_KEYS = (
    "format.fullwidth_space_to_ascii",
    "format.collapse_repeated_ascii_space",
    "format.trim_description_space",
)
PUBLISHER = "local-user-confirmed-space-batch"


def load_preview() -> tuple[dict[str, object], list[dict[str, str]]]:
    manifest = json.loads(PREVIEW_MANIFEST.read_text(encoding="utf-8"))
    activation = json.loads(ACTIVATION_MANIFEST.read_text(encoding="utf-8"))
    replay_manifest = json.loads(REPLAY_MANIFEST.read_text(encoding="utf-8"))
    if manifest.get("rule_status") != "active" or manifest.get("formal_publication") is not False:
        raise SystemExit("Space preview is not an active, unpublished preview.")
    if activation.get("status") != "active" or activation.get("rule_version") != RULE_VERSION:
        raise SystemExit("Space rule activation is not valid.")
    if replay_manifest.get("status") != "passed" or replay_manifest.get("fail_count") != 0:
        raise SystemExit("Space rule replay is not passed.")
    if not PREVIEW_CSV.exists():
        raise SystemExit("Space preview CSV is missing.")
    if sha256_file(PREVIEW_CSV) != manifest.get("preview_sha256"):
        raise SystemExit("Space preview hash does not match its manifest.")
    rows = list(csv.DictReader(PREVIEW_CSV.open(encoding="utf-8-sig", newline="")))
    if len(rows) != 776 or int(manifest.get("preview_rows", 0)) != 776:
        raise SystemExit(f"Space preview row count is {len(rows)}, expected 776.")
    if len({row["CANDIDATE_ID"] for row in rows}) != len(rows):
        raise SystemExit("Space preview contains duplicate candidate identities.")
    if any(row["PREVIEW_STATUS"] != "ready_for_confirmation" for row in rows):
        raise SystemExit("Space preview contains a row that is not ready for confirmation.")
    return manifest, rows


def chunked(values: list[str], size: int = 800):
    for start in range(0, len(values), size):
        yield values[start : start + size]


def main() -> None:
    preview_manifest, preview_rows = load_preview()
    connection = connect_local(DB, timeout=60)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=60000")
    connection.execute("PRAGMA foreign_keys=ON")

    candidate_ids = [row["CANDIDATE_ID"] for row in preview_rows]
    candidate_map: dict[str, sqlite3.Row] = {}
    for part in chunked(candidate_ids):
        marks = ",".join("?" for _ in part)
        rows = connection.execute(
            f"""
            SELECT c.candidate_id,c.batch_id,c.review_state,c.validator_status,
              c.original_description,c.candidate_description,c.validator_version,
              c.publication_state,d.source_snapshot_id,d.source_schema,d.site_id,
              d.asset_number,d.source_row_hash,d.context_hash
            FROM semantic_candidate c
            JOIN device_identity d ON d.device_id=c.device_id
            WHERE c.candidate_id IN ({marks})
            """,
            part,
        ).fetchall()
        candidate_map.update({row["candidate_id"]: row for row in rows})
    if len(candidate_map) != len(candidate_ids):
        raise SystemExit(f"Candidate identity mismatch: {len(candidate_map)} of {len(candidate_ids)} found.")

    batch_ids = sorted({candidate_map[candidate_id]["batch_id"] for candidate_id in candidate_ids})
    if len(batch_ids) != 1:
        raise SystemExit("Space preview spans multiple batches.")
    batch_id = batch_ids[0]
    preview_by_id = {row["CANDIDATE_ID"]: row for row in preview_rows}
    identity_keys: set[tuple[str, str, str]] = set()
    for candidate_id, candidate in candidate_map.items():
        preview = preview_by_id[candidate_id]
        identity = (candidate["source_schema"], candidate["site_id"], candidate["asset_number"])
        if identity in identity_keys:
            raise SystemExit(f"Duplicate source identity in preview: {identity}")
        identity_keys.add(identity)
        if candidate["publication_state"] != "unpublished" or candidate["review_state"] != "pending":
            raise SystemExit(f"Candidate is not pending/unpublished: {candidate_id}")
        if candidate["validator_status"] != "candidate":
            raise SystemExit(f"Candidate is not publishable: {candidate_id}")
        if preview["SITEID"] != candidate["site_id"] or preview["ASSETNUM"] != candidate["asset_number"]:
            raise SystemExit(f"Identity mismatch for {candidate_id}")
        if preview["ORIGINAL_DESCRIPTION"] != candidate["original_description"]:
            raise SystemExit(f"Original description mismatch for {candidate_id}")
        if not preview["PROPOSED_DESCRIPTION"].strip():
            raise SystemExit(f"Empty proposed description: {candidate_id}")
        if any(rule_key not in preview["APPLIED_RULE_KEYS"] for rule_key in ["format.collapse_repeated_ascii_space"]):
            raise SystemExit(f"Expected space rule missing for {candidate_id}")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_dir, backup_files = backup_before_publish(
        connection,
        "space-publication",
        stamp,
        (DUCKDB, DUCKDB_WAL),
    )
    connection.execute("BEGIN IMMEDIATE")
    active_rules = connection.execute(
        "SELECT rule_key,status,version FROM terminology_rule WHERE rule_key IN (?,?,?) ORDER BY rule_key",
        RULE_KEYS,
    ).fetchall()
    if len(active_rules) != 3 or any(row["status"] != "active" or row["version"] != RULE_VERSION for row in active_rules):
        connection.rollback()
        raise SystemExit("All three space rules must be active at the expected version.")
    replay = connection.execute(
        """
        SELECT replay_id,evaluation_count,pass_count,fail_count,status
        FROM replay_run WHERE replay_id=?
        """,
        (json.loads(REPLAY_MANIFEST.read_text(encoding="utf-8"))["replay_id"],),
    ).fetchone()
    if replay is None or replay["status"] != "passed" or replay["fail_count"] != 0 or replay["evaluation_count"] != replay["pass_count"]:
        connection.rollback()
        raise SystemExit("No passed space-rule replay is available.")

    now = utc_now()
    publication_run_id = f"space-publication-{uuid.uuid4().hex}"
    approval_batch_id = f"space-approval-{uuid.uuid4().hex}"
    created_reviews = 0
    inserted_publications = 0
    for candidate_id in sorted(candidate_ids):
        candidate = candidate_map[candidate_id]
        preview = preview_by_id[candidate_id]
        final_description = preview["PROPOSED_DESCRIPTION"]
        review_id = f"review-space-{candidate_id}"
        receipt = f"receipt-space-{candidate_id}"
        existing_review = connection.execute("SELECT * FROM review_decision WHERE candidate_id=?", (candidate_id,)).fetchone()
        if existing_review is not None:
            connection.rollback()
            raise SystemExit(f"Existing review blocks fresh batch approval: {candidate_id}")
        connection.execute(
            """
            INSERT INTO review_decision
              (review_id,candidate_id,decision,reviewed_description,reason_code,review_note,
               reviewer,approval_receipt,reviewed_at)
            VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (
                review_id,
                candidate_id,
                "approved",
                final_description,
                "SPACE_RULE_BATCH_APPROVED",
                f"User confirmed publication of 776 rows; approval batch {approval_batch_id}; rule version {RULE_VERSION}; replay {replay['replay_id']} passed {replay['pass_count']}/{replay['evaluation_count']}; source remains read-only.",
                PUBLISHER,
                receipt,
                now,
            ),
        )
        created_reviews += 1
        publication_hash = sha256_bytes(json.dumps({
            "source_schema": candidate["source_schema"],
            "site_id": candidate["site_id"],
            "asset_number": candidate["asset_number"],
            "source_snapshot_id": candidate["source_snapshot_id"],
            "final_description": final_description,
            "rule_version": RULE_VERSION,
            "applied_rule_keys": preview["APPLIED_RULE_KEYS"],
        }, ensure_ascii=False, sort_keys=True).encode("utf-8"))
        if connection.execute("SELECT 1 FROM published_description WHERE candidate_id=?", (candidate_id,)).fetchone() is not None:
            connection.rollback()
            raise SystemExit(f"Candidate was published concurrently: {candidate_id}")
        connection.execute(
            """
            INSERT INTO published_description
              (publication_id,candidate_id,review_id,source_snapshot_id,source_schema,site_id,
               asset_number,final_description,rule_version,validator_version,replay_id,
               published_by,published_at,publication_hash)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                f"publication-space-{candidate_id}",
                candidate_id,
                review_id,
                candidate["source_snapshot_id"],
                candidate["source_schema"],
                candidate["site_id"],
                candidate["asset_number"],
                final_description,
                RULE_VERSION,
                candidate["validator_version"],
                replay["replay_id"],
                PUBLISHER,
                now,
                publication_hash,
            ),
        )
        connection.execute(
            "UPDATE semantic_candidate SET publication_state='published',review_state='approved' WHERE candidate_id=?",
            (candidate_id,),
        )
        inserted_publications += 1

    current_published = connection.execute("SELECT count(*) FROM published_description").fetchone()[0]
    connection.execute(
        "UPDATE batch_run SET published_count=?,formal_publication=1 WHERE batch_id=?",
        (current_published, batch_id),
    )
    payload = {
        "publication_run_id": publication_run_id,
        "approval_batch_id": approval_batch_id,
        "batch_id": batch_id,
        "preview_id": preview_manifest["preview_id"],
        "preview_rows": len(preview_rows),
        "created_reviews": created_reviews,
        "inserted_publications": inserted_publications,
        "total_published_after": current_published,
        "rule_version": RULE_VERSION,
        "rule_keys": list(RULE_KEYS),
        "replay_id": replay["replay_id"],
        "replay_pass_count": replay["pass_count"],
        "replay_evaluation_count": replay["evaluation_count"],
        "preview_sha256": preview_manifest["preview_sha256"],
        "pre_publication_backup": str(backup_dir),
        "source_snapshot_id": preview_manifest["source_snapshot_id"],
        "source_write": False,
    }
    connection.execute(
        "INSERT INTO audit_event(entity_type,entity_id,event_type,actor,payload_json,event_at) VALUES (?,?,?,?,?,?)",
        ("publication", publication_run_id, "space_preview_published", PUBLISHER, json.dumps(payload, ensure_ascii=False), now),
    )
    connection.execute(
        "INSERT INTO audit_event(entity_type,entity_id,event_type,actor,payload_json,event_at) VALUES (?,?,?,?,?,?)",
        ("approval_batch", approval_batch_id, "space_batch_approval_created", PUBLISHER, json.dumps({"preview_id": preview_manifest["preview_id"], "count": len(preview_rows), "source_write": False}, ensure_ascii=False), now),
    )
    connection.commit()
    publication_manifest = {
        **payload,
        "published_at_utc": now,
        "formal_result_layer": "published_description",
        "formal_publication": True,
        "status": "published",
        "backup_files": [{"path": str(path), "size": path.stat().st_size} for path in backup_files],
    }
    PUBLICATION_MANIFEST.write_text(json.dumps(publication_manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    connection.close()
    print(json.dumps(publication_manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()