"""Replay, isolate anomalies, approve, and publish the safe NFKC batch.

The source MaxiEAM snapshot remains read-only. A full replay is performed
before any candidate is approved. Row-level failures are isolated as
``needs_review``; only rows that pass every gate are approved and published.
"""
from __future__ import annotations

import csv
import hashlib
import json
import shutil
import sqlite3
import unicodedata
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parent
DATA_DIR = ROOT / "data"
DB = DATA_DIR / "semantic_workflow.sqlite3"
DUCKDB = DATA_DIR / "semantic_analytics_v155.duckdb"
DUCKDB_WAL = DATA_DIR / "semantic_analytics_v155.duckdb.wal"
PREVIEW_DIR = PROJECT_ROOT / "pilots" / "HD_SAAS" / "nfkc_safe_format_preview"
PREVIEW_CSV = PREVIEW_DIR / "rewrite_preview.csv"
PREVIEW_MANIFEST = PREVIEW_DIR / "manifest.json"
SAMPLE_MANIFEST = PREVIEW_DIR / "sample_manifest.json"
REPLAY_MANIFEST = PREVIEW_DIR / "replay_manifest.json"
ANOMALY_CSV = PREVIEW_DIR / "anomalies.csv"
ANOMALY_MANIFEST = PREVIEW_DIR / "anomaly_manifest.json"
PUBLICATION_MANIFEST = PREVIEW_DIR / "publication_manifest.json"

EXPECTED_ROWS = 4306
RULE_VERSION = "nfkc-format-proposed-20260812-v1"
VALIDATOR_VERSION = "hd-semantic-validator-0.2.0"
RULE_KEY = "format.nfkc_compatibility_normalization"
PUBLISHER = "local-user-confirmed-nfkc-safe-4306"


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


def load_preview() -> tuple[dict[str, object], dict[str, object], list[dict[str, str]]]:
    manifest = json.loads(PREVIEW_MANIFEST.read_text(encoding="utf-8"))
    sample_manifest = json.loads(SAMPLE_MANIFEST.read_text(encoding="utf-8"))
    if manifest.get("rule_version") != RULE_VERSION:
        raise SystemExit("NFKC preview rule version mismatch.")
    if manifest.get("preview_rows") != EXPECTED_ROWS:
        raise SystemExit(f"NFKC preview must contain {EXPECTED_ROWS} rows.")
    if manifest.get("rule_status") != "proposed_not_active":
        raise SystemExit("NFKC preview is not an unpublished proposed preview.")
    if manifest.get("source_write") is not False or manifest.get("formal_publication") is not False:
        raise SystemExit("NFKC preview is not read-only.")
    if sample_manifest.get("selected_count") != 200:
        raise SystemExit("NFKC sample must contain 200 rows.")
    if sha256_file(PREVIEW_CSV) != manifest.get("preview_sha256"):
        raise SystemExit("NFKC preview hash mismatch.")
    with PREVIEW_CSV.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if len(rows) != EXPECTED_ROWS:
        raise SystemExit(f"NFKC preview CSV contains {len(rows)} rows, expected {EXPECTED_ROWS}.")
    if len({row["CANDIDATE_ID"] for row in rows}) != EXPECTED_ROWS:
        raise SystemExit("NFKC preview contains duplicate candidate IDs.")
    for row in rows:
        if row["SOURCE_WRITE"] != "false" or row["FORMAL_PUBLICATION"] != "false":
            raise SystemExit("NFKC preview contains a writable or published row.")
        if row["PREVIEW_STATUS"] != "proposed_not_active":
            raise SystemExit("NFKC preview contains an unexpected row status.")
    return manifest, sample_manifest, rows


def backup_before_publish(connection: sqlite3.Connection, stamp: str) -> tuple[Path, list[Path]]:
    backup_dir = ROOT / "backups" / f"nfkc-safe-publication-pre-{stamp}"
    backup_dir.mkdir(parents=True, exist_ok=False)
    sqlite_backup = backup_dir / DB.name
    backup_connection = sqlite3.connect(str(sqlite_backup))
    connection.backup(backup_connection)
    backup_connection.close()
    files = [sqlite_backup]
    for source in (DUCKDB, DUCKDB_WAL):
        if source.exists():
            destination = backup_dir / source.name
            shutil.copy2(source, destination)
            files.append(destination)
    (backup_dir / "manifest.json").write_text(
        json.dumps(
            {
                "status": "pre_publication_backup",
                "created_at_utc": utc_now(),
                "files": [{"path": str(path), "size": path.stat().st_size} for path in files],
                "source_write": False,
                "formal_publication": False,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return backup_dir, files


def append_reason(existing: str, reason: str) -> str:
    try:
        values = json.loads(existing or "[]")
        if not isinstance(values, list):
            values = []
    except json.JSONDecodeError:
        values = []
    if reason not in values:
        values.append(reason)
    return json.dumps(values, ensure_ascii=False)


def main() -> None:
    preview_manifest, sample_manifest, preview_rows = load_preview()
    preview_by_id = {row["CANDIDATE_ID"]: row for row in preview_rows}
    candidate_ids = sorted(preview_by_id)

    connection = sqlite3.connect(str(DB), timeout=120)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=120000")
    connection.execute("PRAGMA foreign_keys=ON")

    marks = ",".join("?" for _ in candidate_ids)
    candidates = connection.execute(
        f"""
        SELECT c.candidate_id,c.batch_id,c.original_description,c.candidate_description,
          c.review_state,c.publication_state,c.validator_status,c.confidence,
          c.validator_version,c.reason_codes_json,d.source_snapshot_id,d.source_schema,
          d.site_id,d.asset_number
        FROM semantic_candidate c
        JOIN device_identity d ON d.device_id=c.device_id
        WHERE c.candidate_id IN ({marks})
        """,
        candidate_ids,
    ).fetchall()
    candidate_map = {row["candidate_id"]: row for row in candidates}
    if len(candidate_map) != EXPECTED_ROWS:
        raise SystemExit(f"Database/preview mismatch: {len(candidate_map)} of {EXPECTED_ROWS} candidates found.")

    # Full row-level replay. Failures are collected and isolated; they never
    # enter the approval or publication set.
    failures: list[dict[str, str]] = []
    passed_ids: list[str] = []
    source_identities: dict[tuple[str, str, str], str] = {}
    for candidate_id in candidate_ids:
        preview = preview_by_id[candidate_id]
        candidate = candidate_map[candidate_id]
        reasons: list[str] = []
        identity = (candidate["source_schema"], candidate["site_id"], candidate["asset_number"])
        previous = source_identities.get(identity)
        if previous is not None:
            reasons.append("DUPLICATE_PREVIEW_SOURCE_IDENTITY")
        else:
            source_identities[identity] = candidate_id
        if candidate["review_state"] != "pending":
            reasons.append("CANDIDATE_NOT_PENDING")
        if candidate["publication_state"] != "unpublished":
            reasons.append("CANDIDATE_ALREADY_PUBLISHED")
        if candidate["validator_status"] != "candidate" or candidate["confidence"] != "high":
            reasons.append("CANDIDATE_NOT_HIGH_QUALITY")
        if candidate["original_description"] != preview["ORIGINAL_DESCRIPTION"]:
            reasons.append("ORIGINAL_DESCRIPTION_MISMATCH")
        if candidate["candidate_description"] != preview["EXISTING_CANDIDATE_DESCRIPTION"]:
            reasons.append("EXISTING_CANDIDATE_DESCRIPTION_MISMATCH")
        if unicodedata.normalize("NFKC", preview["ORIGINAL_DESCRIPTION"]) != preview["PROPOSED_DESCRIPTION"]:
            reasons.append("NFKC_TRANSFORM_MISMATCH")
        if "?" in preview["ORIGINAL_DESCRIPTION"] or "？" in preview["ORIGINAL_DESCRIPTION"]:
            reasons.append("QUESTION_MARK_CONTEXT_DEFERRED")
        if "?" in preview["PROPOSED_DESCRIPTION"] or "？" in preview["PROPOSED_DESCRIPTION"]:
            reasons.append("QUESTION_MARK_PROPOSED")
        if not preview["PROPOSED_DESCRIPTION"].strip():
            reasons.append("EMPTY_PROPOSED_DESCRIPTION")
        if connection.execute(
            "SELECT 1 FROM published_description WHERE source_schema=? AND site_id=? AND asset_number=?",
            identity,
        ).fetchone():
            reasons.append("FORMAL_IDENTITY_ALREADY_PUBLISHED")
        if connection.execute(
            "SELECT 1 FROM review_decision WHERE candidate_id=?", (candidate_id,)
        ).fetchone():
            reasons.append("EXISTING_REVIEW_DECISION")
        if reasons:
            failures.append(
                {
                    "CANDIDATE_ID": candidate_id,
                    "SITEID": candidate["site_id"],
                    "ASSETNUM": candidate["asset_number"],
                    "ORIGINAL_DESCRIPTION": candidate["original_description"],
                    "PROPOSED_DESCRIPTION": preview["PROPOSED_DESCRIPTION"],
                    "FAILURE_CODES": "|".join(dict.fromkeys(reasons)),
                }
            )
        else:
            passed_ids.append(candidate_id)

    # Keep the full replay result even when an unexpected row-level failure is
    # found. A second passed replay is created for the exact publish subset.
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_dir, backup_files = backup_before_publish(connection, stamp)
    now = utc_now()
    full_replay_id = f"replay-nfkc-full-{uuid.uuid4().hex}"
    subset_replay_id = f"replay-nfkc-passed-{uuid.uuid4().hex}"
    publication_run_id = f"nfkc-safe-publication-{uuid.uuid4().hex}"
    approval_batch_id = f"nfkc-safe-approval-{uuid.uuid4().hex}"
    batch_ids = {candidate_map[candidate_id]["batch_id"] for candidate_id in candidate_ids}
    if len(batch_ids) != 1:
        raise SystemExit(f"NFKC preview spans multiple batches: {batch_ids}")
    batch_id = next(iter(batch_ids))

    try:
        connection.execute("BEGIN IMMEDIATE")
        full_status = "passed" if not failures else "failed"
        connection.execute(
            """
            INSERT INTO replay_run
              (replay_id,rule_version,validator_version,evaluation_count,pass_count,fail_count,
               status,started_at,finished_at)
            VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (full_replay_id, RULE_VERSION, VALIDATOR_VERSION, EXPECTED_ROWS, len(passed_ids), len(failures), full_status, now, now),
        )
        connection.execute(
            """
            INSERT INTO audit_event(entity_type,entity_id,event_type,actor,payload_json,event_at)
            VALUES (?,?,?,?,?,?)
            """,
            (
                "replay",
                full_replay_id,
                "nfkc_safe_full_replay_completed",
                PUBLISHER,
                json.dumps(
                    {
                        "preview_id": preview_manifest["preview_id"],
                        "evaluation_count": EXPECTED_ROWS,
                        "pass_count": len(passed_ids),
                        "fail_count": len(failures),
                        "source_write": False,
                        "formal_publication": False,
                    },
                    ensure_ascii=False,
                ),
                now,
            ),
        )

        for failure in failures:
            connection.execute(
                """
                UPDATE semantic_candidate
                SET validator_status='needs_review',
                    reason_codes_json=?
                WHERE candidate_id=?
                """,
                (append_reason(candidate_map[failure["CANDIDATE_ID"]]["reason_codes_json"], "NFKC_BATCH_REPLAY_FAILED"), failure["CANDIDATE_ID"]),
            )
        if failures:
            with ANOMALY_CSV.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(failures[0]))
                writer.writeheader()
                writer.writerows(failures)
            connection.execute(
                """
                INSERT INTO audit_event(entity_type,entity_id,event_type,actor,payload_json,event_at)
                VALUES (?,?,?,?,?,?)
                """,
                (
                    "batch",
                    batch_id,
                    "nfkc_replay_anomalies_isolated",
                    PUBLISHER,
                    json.dumps({"count": len(failures), "file": str(ANOMALY_CSV), "source_write": False}, ensure_ascii=False),
                    now,
                ),
            )

        if not passed_ids:
            connection.rollback()
            raise SystemExit("Full NFKC replay produced no publishable rows; all rows were isolated.")

        connection.execute(
            """
            INSERT INTO replay_run
              (replay_id,rule_version,validator_version,evaluation_count,pass_count,fail_count,
               status,started_at,finished_at)
            VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (subset_replay_id, RULE_VERSION, VALIDATOR_VERSION, len(passed_ids), len(passed_ids), 0, "passed", now, now),
        )
        preview_by_id = {row["CANDIDATE_ID"]: row for row in preview_rows}
        for candidate_id in passed_ids:
            candidate = candidate_map[candidate_id]
            preview = preview_by_id[candidate_id]
            review_id = f"review-nfkc-4306-{candidate_id}"
            receipt = f"receipt-nfkc-4306-{candidate_id}"
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
                    "NFKC_FORMAT_BATCH_APPROVED",
                    f"User confirmed the NFKC safe batch; full replay {full_replay_id} evaluated {EXPECTED_ROWS} rows with {len(failures)} isolated anomalies; passed replay {subset_replay_id}; source remains read-only.",
                    PUBLISHER,
                    receipt,
                    now,
                ),
            )
            publication_hash = sha256_bytes(
                json.dumps(
                    {
                        "source_schema": candidate["source_schema"],
                        "site_id": candidate["site_id"],
                        "asset_number": candidate["asset_number"],
                        "source_snapshot_id": candidate["source_snapshot_id"],
                        "final_description": preview["PROPOSED_DESCRIPTION"],
                        "rule_version": RULE_VERSION,
                        "rule_key": RULE_KEY,
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
                    f"publication-nfkc-4306-{candidate_id}",
                    candidate_id,
                    review_id,
                    candidate["source_snapshot_id"],
                    candidate["source_schema"],
                    candidate["site_id"],
                    candidate["asset_number"],
                    preview["PROPOSED_DESCRIPTION"],
                    RULE_VERSION,
                    candidate["validator_version"],
                    subset_replay_id,
                    PUBLISHER,
                    now,
                    publication_hash,
                ),
            )
            connection.execute(
                """
                UPDATE semantic_candidate
                SET publication_state='published',review_state='approved',
                    applied_rule_ids_json=?
                WHERE candidate_id=?
                """,
                (json.dumps([RULE_KEY], ensure_ascii=False), candidate_id),
            )

        current_published = int(connection.execute("SELECT count(*) FROM published_description").fetchone()[0])
        approved_in_batch = int(connection.execute(
            """
            SELECT count(*) FROM semantic_candidate c
            JOIN review_decision r ON r.candidate_id=c.candidate_id
            WHERE c.batch_id=? AND r.decision IN ('approved','modified')
            """,
            (batch_id,),
        ).fetchone()[0])
        pending_anomalies = int(connection.execute(
            "SELECT count(*) FROM semantic_candidate WHERE batch_id=? AND validator_status='needs_review' AND publication_state='unpublished'",
            (batch_id,),
        ).fetchone()[0])
        connection.execute(
            "UPDATE batch_run SET approved_count=?,published_count=?,needs_review_count=?,formal_publication=1 WHERE batch_id=?",
            (approved_in_batch, current_published, pending_anomalies, batch_id),
        )
        payload = {
            "publication_run_id": publication_run_id,
            "approval_batch_id": approval_batch_id,
            "batch_id": batch_id,
            "preview_id": preview_manifest["preview_id"],
            "sample_id": sample_manifest["sample_id"],
            "preview_rows": EXPECTED_ROWS,
            "full_replay_id": full_replay_id,
            "full_replay_pass_count": len(passed_ids),
            "full_replay_fail_count": len(failures),
            "subset_replay_id": subset_replay_id,
            "approved_count": len(passed_ids),
            "inserted_publications": len(passed_ids),
            "isolated_anomaly_count": len(failures),
            "total_published_after": current_published,
            "rule_version": RULE_VERSION,
            "rule_key": RULE_KEY,
            "preview_sha256": preview_manifest["preview_sha256"],
            "pre_publication_backup": str(backup_dir),
            "source_write": False,
        }
        connection.execute(
            """
            INSERT INTO audit_event(entity_type,entity_id,event_type,actor,payload_json,event_at)
            VALUES (?,?,?,?,?,?)
            """,
            ("publication", publication_run_id, "nfkc_safe_batch_published", PUBLISHER, json.dumps(payload, ensure_ascii=False), now),
        )
        connection.execute(
            """
            INSERT INTO audit_event(entity_type,entity_id,event_type,actor,payload_json,event_at)
            VALUES (?,?,?,?,?,?)
            """,
            ("approval_batch", approval_batch_id, "nfkc_safe_batch_approval_created", PUBLISHER, json.dumps({"count": len(passed_ids), "replay_id": subset_replay_id, "source_write": False}, ensure_ascii=False), now),
        )
        connection.commit()
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        connection.close()
        raise

    post = {
        "published_rows_in_run": int(connection.execute("SELECT count(*) FROM published_description WHERE publication_id LIKE 'publication-nfkc-4306-%'").fetchone()[0]),
        "approved_reviews_in_run": int(connection.execute("SELECT count(*) FROM review_decision WHERE review_id LIKE 'review-nfkc-4306-%' AND decision='approved'").fetchone()[0]),
        "published_candidates_in_run": int(connection.execute(f"SELECT count(*) FROM semantic_candidate WHERE candidate_id IN ({marks}) AND publication_state='published' AND review_state='approved'", candidate_ids).fetchone()[0]),
        "isolated_anomalies": int(connection.execute("SELECT count(*) FROM semantic_candidate WHERE batch_id=? AND validator_status='needs_review' AND publication_state='unpublished' AND reason_codes_json LIKE '%NFKC_BATCH_REPLAY_FAILED%'", (batch_id,)).fetchone()[0]),
        "distinct_published_identities_global": int(connection.execute("SELECT count(*) FROM (SELECT DISTINCT source_schema,site_id,asset_number FROM published_description)").fetchone()[0]),
        "formal_total": int(connection.execute("SELECT count(*) FROM published_description").fetchone()[0]),
        "published_descriptions_with_question_mark_in_run": int(connection.execute("SELECT count(*) FROM published_description WHERE publication_id LIKE 'publication-nfkc-4306-%' AND (instr(final_description,'?')>0 OR instr(final_description,'？')>0)").fetchone()[0]),
        "unapproved_formal_rows": int(connection.execute("SELECT count(*) FROM published_description p LEFT JOIN review_decision r ON r.review_id=p.review_id WHERE r.review_id IS NULL OR r.decision NOT IN ('approved','modified')").fetchone()[0]),
        "source_write": False,
    }
    connection.close()
    expected_published = len(passed_ids)
    if post["published_rows_in_run"] != expected_published or post["approved_reviews_in_run"] != expected_published or post["published_candidates_in_run"] != expected_published or post["isolated_anomalies"] != len(failures) or post["distinct_published_identities_global"] != post["formal_total"] or post["published_descriptions_with_question_mark_in_run"] != 0 or post["unapproved_formal_rows"] != 0:
        raise SystemExit(f"Post-publication verification failed: {post}")

    replay_manifest = {
        "status": "passed_subset_published" if failures else "passed",
        "full_replay_id": full_replay_id,
        "full_evaluation_count": EXPECTED_ROWS,
        "full_pass_count": len(passed_ids),
        "full_fail_count": len(failures),
        "subset_replay_id": subset_replay_id,
        "subset_evaluation_count": len(passed_ids),
        "subset_pass_count": len(passed_ids),
        "subset_fail_count": 0,
        "source_write": False,
        "formal_publication": False,
    }
    anomaly_manifest = {
        "status": "isolated" if failures else "none",
        "count": len(failures),
        "file": str(ANOMALY_CSV) if failures else None,
        "reason": "NFKC_BATCH_REPLAY_FAILED",
        "source_write": False,
        "formal_publication": False,
    }
    publication_manifest = {
        **payload,
        "published_at_utc": now,
        "formal_result_layer": "published_description",
        "formal_publication": True,
        "status": "published_with_isolated_anomalies" if failures else "published",
        "post_publication_verification": post,
        "backup_files": [{"path": str(path), "size": path.stat().st_size} for path in backup_files],
    }
    REPLAY_MANIFEST.write_text(json.dumps(replay_manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    ANOMALY_MANIFEST.write_text(json.dumps(anomaly_manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    PUBLICATION_MANIFEST.write_text(json.dumps(publication_manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(publication_manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
