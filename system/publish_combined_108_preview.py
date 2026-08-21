"""Publish the user-confirmed 108-row combined preview.

The source MaxiEAM tables remain read-only. This script only writes the local
SQLite workflow/result layer after validating both preview inputs and their
passed replay gates.
"""
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

ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parent
DB = DEFAULT_WORKFLOW
DUCKDB = DEFAULT_DUCKDB
DUCKDB_WAL = Path(str(DEFAULT_DUCKDB) + ".wal")
PREVIEW_DIR = PROJECT_ROOT / "pilots" / "HD_SAAS" / "combined_108_preview"
PREVIEW_CSV = PREVIEW_DIR / "rewrite_preview.csv"
PREVIEW_MANIFEST = PREVIEW_DIR / "manifest.json"
PUBLICATION_MANIFEST = PREVIEW_DIR / "publication_manifest.json"
PUBLISHER = "local-user-confirmed-combined-108"
EXPECTED_ROWS = 108
EXPECTED_RULES = {
    "safe_punctuation_shape": "format.confirmed_safe_punctuation_shape",
    "terminal_question_mark": "format.remove_terminal_question_mark",
}
RULE_VERSIONS = {
    "safe_punctuation_shape": "ai-confirmed-format-preview-20260812-v1",
    "terminal_question_mark": "terminal-question-proposed-20260812-v1",
}


def load_preview() -> tuple[dict[str, object], list[dict[str, str]]]:
    manifest = json.loads(PREVIEW_MANIFEST.read_text(encoding="utf-8"))
    if (
        manifest.get("preview_rows") != EXPECTED_ROWS
        or manifest.get("formal_publication") is not False
        or manifest.get("source_write") is not False
        or manifest.get("status") != "combined_preview_ready_for_approval"
    ):
        raise SystemExit("Combined preview is not an unpublished approval-ready preview.")
    if not PREVIEW_CSV.exists() or sha256_file(PREVIEW_CSV) != manifest.get("preview_sha256"):
        raise SystemExit("Combined preview hash does not match its manifest.")
    with PREVIEW_CSV.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if len(rows) != EXPECTED_ROWS:
        raise SystemExit(f"Combined preview row count is {len(rows)}, expected {EXPECTED_ROWS}.")
    ids = [row["CANDIDATE_ID"] for row in rows]
    if len(set(ids)) != EXPECTED_ROWS:
        raise SystemExit("Combined preview contains duplicate candidate IDs.")
    if any(row["PREVIEW_STATUS"] not in {"ready_for_replay_not_published", "proposed_not_active"} for row in rows):
        raise SystemExit("Combined preview contains a row that is not ready for publication.")
    if any(row["SOURCE_WRITE"] != "false" or row["FORMAL_PUBLICATION"] != "false" for row in rows):
        raise SystemExit("Combined preview contains a row marked as writable or published.")
    if sum("safe_punctuation_shape" in row["DIFF_CATEGORY"] for row in rows) != 66:
        raise SystemExit("Combined preview safe-punctuation count mismatch.")
    if sum("terminal_question_mark" in row["DIFF_CATEGORY"] for row in rows) != 42:
        raise SystemExit("Combined preview terminal-question count mismatch.")
    return manifest, rows


def main() -> None:
    preview_manifest, preview_rows = load_preview()
    connection = sqlite3.connect(str(DB), timeout=120)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=120000")
    connection.execute("PRAGMA foreign_keys=ON")

    candidate_ids = [row["CANDIDATE_ID"] for row in preview_rows]
    marks = ",".join("?" for _ in candidate_ids)
    candidates = connection.execute(
        f"""
        SELECT c.candidate_id,c.batch_id,c.original_description,c.candidate_description,
          c.review_state,c.publication_state,c.validator_status,c.confidence,c.validator_version,
          d.source_snapshot_id,d.source_schema,d.site_id,d.asset_number
        FROM semantic_candidate c
        JOIN device_identity d ON d.device_id=c.device_id
        WHERE c.candidate_id IN ({marks})
        """,
        candidate_ids,
    ).fetchall()
    candidate_map = {row["candidate_id"]: row for row in candidates}
    if len(candidate_map) != EXPECTED_ROWS:
        raise SystemExit(f"Database/preview identity mismatch: {len(candidate_map)} of {EXPECTED_ROWS}.")

    preview_by_id = {row["CANDIDATE_ID"]: row for row in preview_rows}
    identities: set[tuple[str, str, str]] = set()
    for candidate_id, candidate in candidate_map.items():
        preview = preview_by_id[candidate_id]
        identity = (candidate["source_schema"], candidate["site_id"], candidate["asset_number"])
        if identity in identities:
            raise SystemExit(f"Duplicate source identity in preview: {identity}")
        identities.add(identity)
        if candidate["publication_state"] != "unpublished" or candidate["review_state"] != "pending":
            raise SystemExit(f"Candidate is no longer pending/unpublished: {candidate_id}")
        if candidate["validator_status"] != "candidate" or candidate["confidence"] != "high":
            raise SystemExit(f"Candidate is not high-quality publishable: {candidate_id}")
        if candidate["original_description"] != preview["ORIGINAL_DESCRIPTION"]:
            raise SystemExit(f"Original description mismatch: {candidate_id}")
        if candidate["candidate_description"] != preview["EXISTING_CANDIDATE_DESCRIPTION"]:
            raise SystemExit(f"Existing candidate description mismatch: {candidate_id}")
        if not preview["PROPOSED_DESCRIPTION"].strip():
            raise SystemExit(f"Empty proposed description: {candidate_id}")
        if connection.execute(
            "SELECT 1 FROM published_description WHERE source_schema=? AND site_id=? AND asset_number=?",
            identity,
        ).fetchone():
            raise SystemExit(f"Identity already published: {identity}")
        if connection.execute(
            "SELECT 1 FROM review_decision WHERE candidate_id=?", (candidate_id,)
        ).fetchone():
            raise SystemExit(f"Existing review blocks fresh batch approval: {candidate_id}")

    replay_ids = [
        item["replay_id"] for item in json.loads(PREVIEW_MANIFEST.read_text(encoding="utf-8"))["source_previews"]
    ]
    replay_rows = connection.execute(
        f"""
        SELECT replay_id,evaluation_count,pass_count,fail_count,status
        FROM replay_run WHERE replay_id IN ({','.join('?' for _ in replay_ids)})
        """,
        replay_ids,
    ).fetchall()
    replay_map = {row["replay_id"]: row for row in replay_rows}
    if len(replay_map) != 2 or any(
        row["status"] != "passed" or row["fail_count"] != 0 or row["evaluation_count"] != row["pass_count"]
        for row in replay_map.values()
    ):
        raise SystemExit("Both source replay gates must pass before publication.")

    batch_ids = {candidate_map[candidate_id]["batch_id"] for candidate_id in candidate_ids}
    if len(batch_ids) != 1:
        raise SystemExit(f"Preview spans multiple batches: {batch_ids}")
    batch_id = next(iter(batch_ids))
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_dir, backup_files = backup_before_publish(
        connection,
        "combined-108-publication",
        stamp,
        (DUCKDB, DUCKDB_WAL),
    )
    now = utc_now()
    publication_run_id = f"combined-108-publication-{uuid.uuid4().hex}"
    approval_batch_id = f"combined-108-approval-{uuid.uuid4().hex}"
    created_reviews = 0
    inserted_publications = 0

    try:
        connection.execute("BEGIN IMMEDIATE")
        for candidate_id in sorted(candidate_ids):
            candidate = candidate_map[candidate_id]
            preview = preview_by_id[candidate_id]
            diff_category = preview["DIFF_CATEGORY"]
            if "safe_punctuation_shape" in diff_category:
                reason_code = "AI_CONFIRMED_SAFE_PUNCTUATION"
                rule_key = EXPECTED_RULES["safe_punctuation_shape"]
            elif "terminal_question_mark" in diff_category:
                reason_code = "USER_CONFIRMED_TERMINAL_QUESTION_MARK"
                rule_key = EXPECTED_RULES["terminal_question_mark"]
            else:
                connection.rollback()
                raise SystemExit(f"Unknown combined rule category: {candidate_id}")
            review_id = f"review-combined-108-{candidate_id}"
            receipt = f"receipt-combined-108-{candidate_id}"
            review_note = (
                f"User confirmed publication of 108 rows; approval batch {approval_batch_id}; "
                f"rule {rule_key}; source previews and replay gates passed; source remains read-only."
            )
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
                    preview["PROPOSED_DESCRIPTION"],
                    reason_code,
                    review_note,
                    PUBLISHER,
                    receipt,
                    now,
                ),
            )
            created_reviews += 1
            publication_hash = sha256_bytes(
                json.dumps(
                    {
                        "source_schema": candidate["source_schema"],
                        "site_id": candidate["site_id"],
                        "asset_number": candidate["asset_number"],
                        "source_snapshot_id": candidate["source_snapshot_id"],
                        "final_description": preview["PROPOSED_DESCRIPTION"],
                        "rule_key": rule_key,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ).encode("utf-8")
            )
            connection.execute(
                """
                INSERT INTO published_description
                  (publication_id,candidate_id,review_id,source_snapshot_id,source_schema,site_id,
                   asset_number,final_description,rule_version,validator_version,replay_id,
                   published_by,published_at,publication_hash)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    f"publication-combined-108-{candidate_id}",
                    candidate_id,
                    review_id,
                    candidate["source_snapshot_id"],
                    candidate["source_schema"],
                    candidate["site_id"],
                    candidate["asset_number"],
                    preview["PROPOSED_DESCRIPTION"],
                    RULE_VERSIONS["safe_punctuation_shape"] if rule_key == EXPECTED_RULES["safe_punctuation_shape"] else RULE_VERSIONS["terminal_question_mark"],
                    candidate["validator_version"],
                    replay_ids[0] if rule_key == EXPECTED_RULES["safe_punctuation_shape"] else replay_ids[1],
                    PUBLISHER,
                    now,
                    publication_hash,
                ),
            )
            connection.execute(
                "UPDATE semantic_candidate SET publication_state='published',review_state='approved',applied_rule_ids_json=? WHERE candidate_id=?",
                (json.dumps([rule_key], ensure_ascii=False), candidate_id),
            )
            inserted_publications += 1

        current_published = int(connection.execute("SELECT count(*) FROM published_description").fetchone()[0])
        connection.execute(
            "UPDATE batch_run SET published_count=?,formal_publication=1 WHERE batch_id=?",
            (current_published, batch_id),
        )
        replay_summary = [
            {
                "replay_id": row["replay_id"],
                "evaluation_count": row["evaluation_count"],
                "pass_count": row["pass_count"],
                "fail_count": row["fail_count"],
            }
            for row in replay_map.values()
        ]
        payload = {
            "publication_run_id": publication_run_id,
            "approval_batch_id": approval_batch_id,
            "batch_id": batch_id,
            "preview_id": preview_manifest["preview_id"],
            "preview_rows": EXPECTED_ROWS,
            "created_reviews": created_reviews,
            "inserted_publications": inserted_publications,
            "total_published_after": current_published,
            "rule_versions": preview_manifest["rule_versions"],
            "replays": replay_summary,
            "preview_sha256": preview_manifest["preview_sha256"],
            "pre_publication_backup": str(backup_dir),
            "source_write": False,
        }
        connection.execute(
            "INSERT INTO audit_event(entity_type,entity_id,event_type,actor,payload_json,event_at) VALUES (?,?,?,?,?,?)",
            ("publication", publication_run_id, "combined_108_preview_published", PUBLISHER, json.dumps(payload, ensure_ascii=False), now),
        )
        connection.execute(
            "INSERT INTO audit_event(entity_type,entity_id,event_type,actor,payload_json,event_at) VALUES (?,?,?,?,?,?)",
            ("approval_batch", approval_batch_id, "combined_108_batch_approval_created", PUBLISHER, json.dumps({"preview_id": preview_manifest["preview_id"], "count": EXPECTED_ROWS, "source_write": False}, ensure_ascii=False), now),
        )
        connection.commit()
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        connection.close()
        raise

    post = {
        "published_rows_in_run": int(connection.execute(
            "SELECT count(*) FROM published_description WHERE publication_id LIKE 'publication-combined-108-%'"
        ).fetchone()[0]),
        "approved_reviews_in_run": int(connection.execute(
            "SELECT count(*) FROM review_decision WHERE review_id LIKE 'review-combined-108-%' AND decision='approved'"
        ).fetchone()[0]),
        "published_candidates_in_run": int(connection.execute(
            "SELECT count(*) FROM semantic_candidate WHERE candidate_id IN ({}) AND publication_state='published' AND review_state='approved'".format(",".join("?" for _ in candidate_ids)),
            candidate_ids,
        ).fetchone()[0]),
        "distinct_published_identities": int(connection.execute(
            "SELECT count(*) FROM (SELECT DISTINCT source_schema,site_id,asset_number FROM published_description WHERE publication_id LIKE 'publication-combined-108-%')"
        ).fetchone()[0]),
        "published_descriptions_with_question_mark": int(connection.execute(
            "SELECT count(*) FROM published_description WHERE publication_id LIKE 'publication-combined-108-%' AND instr(final_description,'?')>0"
        ).fetchone()[0]),
        "source_write": False,
    }
    connection.close()
    if post["published_rows_in_run"] != EXPECTED_ROWS or post["approved_reviews_in_run"] != EXPECTED_ROWS or post["published_candidates_in_run"] != EXPECTED_ROWS or post["distinct_published_identities"] != EXPECTED_ROWS or post["published_descriptions_with_question_mark"] != 0:
        raise SystemExit(f"Post-publication verification failed: {post}")

    publication_manifest = {
        **payload,
        "published_at_utc": now,
        "formal_result_layer": "published_description",
        "formal_publication": True,
        "status": "published",
        "post_publication_verification": post,
        "backup_files": [{"path": str(path), "size": path.stat().st_size} for path in backup_files],
    }
    PUBLICATION_MANIFEST.write_text(
        json.dumps(publication_manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(publication_manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
