"""Publish the confirmed format-rule preview into the local formal layer."""
from __future__ import annotations

import csv
import json
import sqlite3
import uuid
from pathlib import Path

from common import DEFAULT_WORKFLOW, sha256_bytes, utc_now

ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parent
DB = DEFAULT_WORKFLOW
PREVIEW_DIR = PROJECT_ROOT / "pilots" / "HD_SAAS" / "format_rule_preview"
PREVIEW_CSV = PREVIEW_DIR / "format_rule_rewrite_preview.csv"
PREVIEW_MANIFEST = PREVIEW_DIR / "manifest.json"
PUBLICATION_MANIFEST = PREVIEW_DIR / "publication_manifest.json"
RULE_VERSION = "sample-learned-20260812-v1"
RULE_KEYS = (
    "format.fullwidth_parenthesis_to_ascii",
    "format.fullwidth_comma_to_ascii",
)
PUBLISHER = "local-user-confirmed-format-batch"


def load_preview() -> tuple[dict[str, object], list[dict[str, str]]]:
    manifest = json.loads(PREVIEW_MANIFEST.read_text(encoding="utf-8"))
    if manifest.get("rule_status") != "active":
        raise SystemExit("Preview rules are not active.")
    if manifest.get("source_write") is not False or manifest.get("formal_publication") is not False:
        raise SystemExit("Preview manifest is not read-only.")
    if int(manifest.get("preview_rows", 0)) != 6810:
        raise SystemExit("Preview row count is not the confirmed 6810.")
    if not PREVIEW_CSV.exists() or not PREVIEW_MANIFEST.exists():
        raise SystemExit("Preview files are missing.")
    rows: list[dict[str, str]] = []
    with PREVIEW_CSV.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if len(rows) != 6810:
        raise SystemExit(f"Preview CSV has {len(rows)} rows, expected 6810.")
    if len({row["CANDIDATE_ID"] for row in rows}) != len(rows):
        raise SystemExit("Preview contains duplicate candidate identities.")
    return manifest, rows


def chunked(values: list[str], size: int = 800):
    for start in range(0, len(values), size):
        yield values[start:start + size]


def main() -> None:
    manifest, preview_rows = load_preview()
    connection = sqlite3.connect(DB, timeout=60)
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
              c.original_description,c.candidate_description,c.rule_version,
              c.validator_version,c.publication_state,
              d.source_snapshot_id,d.source_schema,d.site_id,d.asset_number,
              d.source_row_hash,d.context_hash
            FROM semantic_candidate c
            JOIN device_identity d ON d.device_id=c.device_id
            WHERE c.candidate_id IN ({marks})
            """,
            part,
        ).fetchall()
        candidate_map.update({row["candidate_id"]: row for row in rows})
    if len(candidate_map) != len(candidate_ids):
        raise SystemExit(f"Candidate identity mismatch: {len(candidate_map)} of {len(candidate_ids)} found.")

    connection.execute("BEGIN IMMEDIATE")
    active_rules = connection.execute(
        "SELECT rule_key,status FROM terminology_rule WHERE rule_key IN (?,?) ORDER BY rule_key",
        RULE_KEYS,
    ).fetchall()
    if len(active_rules) != 2 or any(row["status"] != "active" for row in active_rules):
        connection.rollback()
        raise SystemExit("Both format rules must be active.")
    replay = connection.execute(
        """
        SELECT replay_id,evaluation_count,pass_count,fail_count,status
        FROM replay_run
        WHERE replay_id LIKE 'replay-activation-%'
        ORDER BY started_at DESC LIMIT 1
        """
    ).fetchone()
    if replay is None or replay["status"] != "passed" or replay["fail_count"] != 0 or replay["evaluation_count"] != replay["pass_count"]:
        connection.rollback()
        raise SystemExit("No passed activation replay is available.")

    preview_by_id = {row["CANDIDATE_ID"]: row for row in preview_rows}
    identity_keys: set[tuple[str, str]] = set()
    for candidate_id, candidate in candidate_map.items():
        preview = preview_by_id[candidate_id]
        identity = (candidate["source_schema"], candidate["site_id"], candidate["asset_number"])
        if identity in identity_keys:
            connection.rollback()
            raise SystemExit(f"Duplicate source identity in preview: {identity}")
        identity_keys.add(identity)
        if preview["SITEID"] != candidate["site_id"] or preview["ASSETNUM"] != candidate["asset_number"]:
            connection.rollback()
            raise SystemExit(f"Identity mismatch for {candidate_id}")
        if candidate["original_description"] != preview["ORIGINAL_DESCRIPTION"]:
            connection.rollback()
            raise SystemExit(f"Original description mismatch for {candidate_id}")
        if candidate["validator_status"] == "blocked":
            connection.rollback()
            raise SystemExit(f"Blocked candidate cannot be published: {candidate_id}")
        if not preview["PROPOSED_DESCRIPTION"].strip():
            connection.rollback()
            raise SystemExit(f"Empty proposed description: {candidate_id}")

    now = utc_now()
    publication_run_id = f"format-publication-{uuid.uuid4().hex}"
    created_reviews = 0
    reused_reviews = 0
    inserted_publications = 0
    existing_publications = 0

    for candidate_id in sorted(candidate_ids):
        candidate = candidate_map[candidate_id]
        preview = preview_by_id[candidate_id]
        final_description = preview["PROPOSED_DESCRIPTION"]
        existing_review = connection.execute(
            "SELECT * FROM review_decision WHERE candidate_id=?",
            (candidate_id,),
        ).fetchone()
        if existing_review is None:
            review_id = f"review-format-{candidate_id}"
            receipt = f"receipt-format-{candidate_id}"
            connection.execute(
                """
                INSERT INTO review_decision
                  (review_id,candidate_id,decision,reviewed_description,reason_code,
                   review_note,reviewer,approval_receipt,reviewed_at)
                VALUES (?,?,?,?,?,?,?,?,?)
                """,
                (
                    review_id,
                    candidate_id,
                    "approved",
                    final_description,
                    "FORMAT_RULE_BATCH_APPROVED",
                    f"用户确认写入 6810 条；规则版本 {RULE_VERSION}；命中规则 {preview['APPLIED_RULE_KEYS']}；300/300 回放通过。",
                    PUBLISHER,
                    receipt,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO audit_event(entity_type,entity_id,event_type,actor,payload_json,event_at)
                VALUES (?,?,?,?,?,?)
                """,
                (
                    "review",
                    review_id,
                    "format_batch_review_created",
                    PUBLISHER,
                    json.dumps({"candidate_id": candidate_id, "publication_run_id": publication_run_id, "rule_version": RULE_VERSION}, ensure_ascii=False),
                    now,
                ),
            )
            created_reviews += 1
        else:
            if existing_review["decision"] not in {"approved", "modified"}:
                connection.rollback()
                raise SystemExit(f"Existing non-approval blocks publication: {candidate_id}")
            existing_description = (existing_review["reviewed_description"] or "").strip()
            if existing_description != final_description:
                connection.rollback()
                raise SystemExit(f"Existing approval description conflicts with preview: {candidate_id}")
            review_id = existing_review["review_id"]
            reused_reviews += 1

        publication_hash = sha256_bytes(json.dumps({
            "source_schema": candidate["source_schema"],
            "site_id": candidate["site_id"],
            "asset_number": candidate["asset_number"],
            "source_snapshot_id": candidate["source_snapshot_id"],
            "final_description": final_description,
            "rule_version": RULE_VERSION,
        }, ensure_ascii=False, sort_keys=True).encode("utf-8"))
        existing_publication = connection.execute(
            "SELECT publication_id,final_description,publication_hash FROM published_description WHERE candidate_id=?",
            (candidate_id,),
        ).fetchone()
        if existing_publication is not None:
            if existing_publication["final_description"] != final_description or existing_publication["publication_hash"] != publication_hash:
                connection.rollback()
                raise SystemExit(f"Existing publication conflicts with preview: {candidate_id}")
            existing_publications += 1
            continue

        connection.execute(
            """
            INSERT INTO published_description
              (publication_id,candidate_id,review_id,source_snapshot_id,source_schema,
               site_id,asset_number,final_description,rule_version,validator_version,
               replay_id,published_by,published_at,publication_hash)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                f"publication-format-{candidate_id}",
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
            "UPDATE semantic_candidate SET publication_state='published',review_state=CASE WHEN review_state='pending' THEN 'approved' ELSE review_state END WHERE candidate_id=?",
            (candidate_id,),
        )
        inserted_publications += 1

    batch_ids = sorted({candidate_map[candidate_id]["batch_id"] for candidate_id in candidate_ids})
    if len(batch_ids) != 1:
        connection.rollback()
        raise SystemExit("Preview spans multiple batches.")
    batch_id = batch_ids[0]
    connection.execute(
        """
        UPDATE batch_run SET
          published_count=(SELECT count(*) FROM published_description p JOIN semantic_candidate c ON c.candidate_id=p.candidate_id WHERE c.batch_id=?),
          formal_publication=1
        WHERE batch_id=?
        """,
        (batch_id, batch_id),
    )
    payload = {
        "publication_run_id": publication_run_id,
        "batch_id": batch_id,
        "preview_rows": len(preview_rows),
        "inserted_publications": inserted_publications,
        "existing_publications": existing_publications,
        "created_reviews": created_reviews,
        "reused_reviews": reused_reviews,
        "rule_version": RULE_VERSION,
        "rule_keys": list(RULE_KEYS),
        "replay_id": replay["replay_id"],
        "source_write": False,
        "source_snapshot_sha256": manifest["source_sha256"],
    }
    connection.execute(
        """
        INSERT INTO audit_event(entity_type,entity_id,event_type,actor,payload_json,event_at)
        VALUES (?,?,?,?,?,?)
        """,
        ("publication", publication_run_id, "format_preview_published", PUBLISHER, json.dumps(payload, ensure_ascii=False), now),
    )
    connection.commit()
    publication_manifest = {
        **payload,
        "published_at_utc": now,
        "formal_result_layer": "published_description",
        "formal_publication": True,
        "source_write": False,
        "status": "published",
    }
    PUBLICATION_MANIFEST.write_text(json.dumps(publication_manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    connection.close()
    print(json.dumps(publication_manifest, ensure_ascii=False))


if __name__ == "__main__":
    main()
