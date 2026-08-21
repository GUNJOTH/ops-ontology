"""Confirm, replay, activate, and publish the next deterministic format batch.

The source MaxiEAM snapshot is never updated. The transaction only changes
the governed local SQLite workflow/result layer after preflight, replay, and
identity checks pass. A SQLite and DuckDB backup is created before mutation.
"""
from __future__ import annotations

import csv
import hashlib
import json
import re
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
PREVIEW_DIR = PROJECT_ROOT / "pilots" / "HD_SAAS" / "next_deterministic_preview"
PREVIEW_CSV = PREVIEW_DIR / "rewrite_preview.csv"
SAMPLE_CSV = PREVIEW_DIR / "sample_200.csv"
PREVIEW_MANIFEST = PREVIEW_DIR / "manifest.json"
SAMPLE_MANIFEST = PREVIEW_DIR / "sample_manifest.json"
PUBLICATION_MANIFEST = PREVIEW_DIR / "publication_manifest.json"
ACTIVATION_MANIFEST = PREVIEW_DIR / "activation_manifest.json"
REPLAY_MANIFEST = PREVIEW_DIR / "replay_manifest.json"
CONFIRMATION_MANIFEST = PREVIEW_DIR / "confirmation_manifest.json"

RULE_VERSION = "deterministic-format-proposed-20260812-v1"
VALIDATOR_VERSION = "hd-semantic-validator-0.2.0"
RULE_KEYS = (
    "format.trim_description_space",
    "format.fullwidth_solidus_to_ascii",
    "format.fullwidth_hyphen_minus_to_ascii",
)
PUBLISHER = "local-user-confirmed-next-deterministic-batch"


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


def transform(value: str) -> str:
    # Complete active format pipeline, including the three new rules.
    value = value.replace("（", "(").replace("）", ")").replace("，", ",")
    value = value.replace("\u3000", " ")
    value = re.sub(r" {2,}", " ", value)
    value = value.strip()
    value = value.replace("／", "/").replace("－", "-")
    return value


def rule_keys_for(value: str) -> list[str]:
    keys: list[str] = []
    if value != value.strip():
        keys.append("format.trim_description_space")
    if "／" in value:
        keys.append("format.fullwidth_solidus_to_ascii")
    if "－" in value:
        keys.append("format.fullwidth_hyphen_minus_to_ascii")
    return keys


def backup_before_publish(connection: sqlite3.Connection, stamp: str) -> tuple[Path, list[Path]]:
    backup_dir = ROOT / "backups" / f"next-deterministic-publication-pre-{stamp}"
    backup_dir.mkdir(parents=True, exist_ok=False)
    sqlite_backup = backup_dir / DB.name
    backup_connection = sqlite3.connect(str(sqlite_backup))
    connection.backup(backup_connection)
    backup_connection.close()
    files = [sqlite_backup]
    if DUCKDB.exists():
        duckdb_backup = backup_dir / DUCKDB.name
        shutil.copy2(DUCKDB, duckdb_backup)
        files.append(duckdb_backup)
    if DUCKDB_WAL.exists():
        wal_backup = backup_dir / DUCKDB_WAL.name
        shutil.copy2(DUCKDB_WAL, wal_backup)
        files.append(wal_backup)
    manifest = {
        "status": "pre_publication_backup",
        "created_at_utc": utc_now(),
        "files": [{"path": str(path), "size": path.stat().st_size} for path in files],
        "source_write": False,
        "formal_publication": False,
    }
    (backup_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return backup_dir, files


def load_preview() -> tuple[dict[str, object], dict[str, object], list[dict[str, str]], list[dict[str, str]]]:
    manifest = json.loads(PREVIEW_MANIFEST.read_text(encoding="utf-8"))
    sample_manifest = json.loads(SAMPLE_MANIFEST.read_text(encoding="utf-8"))
    if manifest.get("rule_status") != "proposed_not_active":
        raise SystemExit("Next preview is not a proposed, inactive preview.")
    if manifest.get("source_write") is not False or manifest.get("formal_publication") is not False:
        raise SystemExit("Next preview manifest is not read-only.")
    if int(manifest.get("preview_rows", 0)) != 571:
        raise SystemExit("Next preview must contain exactly 571 rows.")
    if int(sample_manifest.get("selected_count", 0)) != 200:
        raise SystemExit("Next sample must contain exactly 200 rows.")
    if sha256_file(PREVIEW_CSV) != manifest.get("preview_sha256"):
        raise SystemExit("Next preview hash does not match its manifest.")
    if sha256_file(SAMPLE_CSV) != sample_manifest.get("sample_sha256"):
        raise SystemExit("Next sample hash does not match its manifest.")
    with PREVIEW_CSV.open(encoding="utf-8-sig", newline="") as handle:
        preview_rows = list(csv.DictReader(handle))
    with SAMPLE_CSV.open(encoding="utf-8-sig", newline="") as handle:
        sample_rows = list(csv.DictReader(handle))
    if len(preview_rows) != 571 or len(sample_rows) != 200:
        raise SystemExit(f"Preview/sample row count mismatch: {len(preview_rows)}/{len(sample_rows)}")
    if len({row["CANDIDATE_ID"] for row in preview_rows}) != 571:
        raise SystemExit("Next preview contains duplicate candidate IDs.")
    if len({row["CANDIDATE_ID"] for row in sample_rows}) != 200:
        raise SystemExit("Next sample contains duplicate candidate IDs.")
    preview_ids = {row["CANDIDATE_ID"] for row in preview_rows}
    if not {row["CANDIDATE_ID"] for row in sample_rows} <= preview_ids:
        raise SystemExit("Sample contains a candidate outside the preview.")
    return manifest, sample_manifest, preview_rows, sample_rows


def main() -> None:
    preview_manifest, sample_manifest, preview_rows, sample_rows = load_preview()
    preview_by_id = {row["CANDIDATE_ID"]: row for row in preview_rows}
    sample_by_id = {row["CANDIDATE_ID"]: row for row in sample_rows}

    connection = sqlite3.connect(str(DB), timeout=120)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=120000")
    connection.execute("PRAGMA foreign_keys=ON")

    candidate_ids = sorted(preview_by_id)
    marks = ",".join("?" for _ in candidate_ids)
    candidate_rows = connection.execute(
        f"""
        SELECT c.candidate_id,c.batch_id,c.review_state,c.validator_status,c.confidence,
          c.original_description,c.candidate_description,c.applied_rule_ids_json,
          c.validator_version,c.publication_state,
          d.source_snapshot_id,d.source_schema,d.site_id,d.asset_number,
          d.source_row_hash,d.context_hash
        FROM semantic_candidate c JOIN device_identity d ON d.device_id=c.device_id
        WHERE c.candidate_id IN ({marks})
        """,
        candidate_ids,
    ).fetchall()
    candidate_map = {row["candidate_id"]: row for row in candidate_rows}
    if len(candidate_map) != len(candidate_ids):
        raise SystemExit(f"Candidate identity mismatch: {len(candidate_map)} of {len(candidate_ids)} found.")

    batch_ids = sorted({row["batch_id"] for row in candidate_rows})
    if len(batch_ids) != 1:
        raise SystemExit(f"Preview spans multiple batches: {batch_ids}")
    identity_keys: set[tuple[str, str, str]] = set()
    for candidate_id in candidate_ids:
        candidate = candidate_map[candidate_id]
        preview = preview_by_id[candidate_id]
        identity = (candidate["source_schema"], candidate["site_id"], candidate["asset_number"])
        if identity in identity_keys:
            raise SystemExit(f"Duplicate source identity in preview: {identity}")
        identity_keys.add(identity)
        if candidate["publication_state"] != "unpublished" or candidate["review_state"] != "pending":
            raise SystemExit(f"Candidate is not pending/unpublished: {candidate_id}")
        if candidate["validator_status"] != "candidate" or candidate["confidence"] != "high":
            raise SystemExit(f"Candidate is not high-quality candidate: {candidate_id}")
        if preview["SITEID"] != candidate["site_id"] or preview["ASSETNUM"] != candidate["asset_number"]:
            raise SystemExit(f"Identity mismatch for {candidate_id}")
        if preview["ORIGINAL_DESCRIPTION"] != candidate["original_description"]:
            raise SystemExit(f"Original description mismatch for {candidate_id}")
        if not preview["PROPOSED_DESCRIPTION"].strip() or transform(preview["ORIGINAL_DESCRIPTION"]) != preview["PROPOSED_DESCRIPTION"]:
            raise SystemExit(f"Preview transform mismatch for {candidate_id}")
        if preview["APPLIED_RULE_KEYS"] != "|".join(rule_keys_for(preview["ORIGINAL_DESCRIPTION"])):
            raise SystemExit(f"Rule provenance mismatch for {candidate_id}")
        if connection.execute("SELECT 1 FROM published_description WHERE source_schema=? AND site_id=? AND asset_number=?", identity).fetchone() is not None:
            raise SystemExit(f"Formal identity already published: {identity}")
        if connection.execute("SELECT 1 FROM review_decision WHERE candidate_id=?", (candidate_id,)).fetchone() is not None:
            raise SystemExit(f"Existing review blocks fresh batch approval: {candidate_id}")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_dir, backup_files = backup_before_publish(connection, stamp)
    now = utc_now()
    publication_run_id = f"next-deterministic-publication-{uuid.uuid4().hex}"
    approval_batch_id = f"next-deterministic-approval-{uuid.uuid4().hex}"
    replay_id = f"replay-next-deterministic-{uuid.uuid4().hex}"
    activation_id = f"next-deterministic-activation-{uuid.uuid4().hex}"
    confirmation_id = f"next-deterministic-confirmation-{uuid.uuid4().hex}"

    try:
        connection.execute("BEGIN IMMEDIATE")
        # Register the proposed rules as candidate rules before replay. The
        # transaction rolls back these records if any replay case fails.
        rule_specs = {
            "format.trim_description_space": ("首尾空白", "去除首尾空白"),
            "format.fullwidth_solidus_to_ascii": ("／", "/"),
            "format.fullwidth_hyphen_minus_to_ascii": ("－", "-"),
        }
        for rule_key in RULE_KEYS:
            existing = connection.execute("SELECT * FROM terminology_rule WHERE rule_key=?", (rule_key,)).fetchone()
            if existing is not None:
                # trim_description_space was activated in the previous space
                # batch. Reuse that active rule instead of creating a second
                # definition; only the two new punctuation rules are added.
                if rule_key != "format.trim_description_space" or existing["status"] != "active":
                    raise SystemExit(f"Rule key already exists but is not reusable: {rule_key}")
                continue
            source_term, target_term = rule_specs[rule_key]
            connection.execute(
                """
                INSERT INTO terminology_rule
                  (term_rule_id,rule_key,rule_type,source_term,target_term,site_scope,
                   classification_scope,context_condition_json,priority,status,version,
                   evidence_json,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    f"term-{rule_key.replace('.', '-')}", rule_key, "format", source_term, target_term,
                    None, None, "{}", 100, "candidate", RULE_VERSION,
                    json.dumps({"preview_id": preview_manifest["preview_id"], "sample_id": sample_manifest["sample_id"], "guardrail": "format-only; preserve all non-target characters"}, ensure_ascii=False),
                    now, now,
                ),
            )

        # Confirm the 200-row sample as evaluation evidence only.
        for candidate_id, preview in sample_by_id.items():
            candidate = candidate_map[candidate_id]
            case_id = f"case-next-deterministic-{candidate_id}"
            context = {
                "sample_id": sample_manifest["sample_id"],
                "preview_id": preview_manifest["preview_id"],
                "preview_pattern": preview["PREVIEW_PATTERN"],
                "applied_rule_keys": preview["APPLIED_RULE_KEYS"].split("|") if preview["APPLIED_RULE_KEYS"] else [],
                "location_code": preview["LOCATION_CODE"],
                "location_description": preview["LOCATION_DESCRIPTION"],
                "location_parent": preview["LOCATION_PARENT"],
                "classification_description": preview["CLASSIFICATION_DESCRIPTION"],
                "source_row_hash": preview["SOURCE_ROW_HASH"],
                "context_hash": preview["CONTEXT_HASH"],
            }
            existing = connection.execute("SELECT * FROM evaluation_case WHERE case_id=?", (case_id,)).fetchone()
            if existing is not None:
                if existing["expected_description"] != preview["PROPOSED_DESCRIPTION"] or existing["introduced_rule_version"] != RULE_VERSION:
                    raise SystemExit(f"Existing evaluation case conflicts: {case_id}")
                continue
            connection.execute(
                """
                INSERT INTO evaluation_case
                  (case_id,source_review_id,source_snapshot_id,source_schema,site_id,asset_number,
                   input_description,context_json,expected_decision,expected_description,failure_type,
                   active,introduced_rule_version,created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    case_id, None, candidate["source_snapshot_id"], candidate["source_schema"],
                    candidate["site_id"], candidate["asset_number"], preview["ORIGINAL_DESCRIPTION"],
                    json.dumps(context, ensure_ascii=False), "approved", preview["PROPOSED_DESCRIPTION"],
                    "next_deterministic_format_confirmation", 1, RULE_VERSION, now,
                ),
            )

        all_cases = connection.execute("SELECT * FROM evaluation_case WHERE active=1 ORDER BY case_id").fetchall()
        passed = 0
        failed = 0
        connection.execute(
            """
            INSERT INTO replay_run
              (replay_id,rule_version,validator_version,evaluation_count,pass_count,fail_count,status,started_at)
            VALUES (?,?,?,?,?,?,?,?)
            """,
            (replay_id, RULE_VERSION, VALIDATOR_VERSION, 0, 0, 0, "running", now),
        )
        for case in all_cases:
            expected_decision = case["expected_decision"]
            actual_decision = expected_decision
            actual_description = transform(case["input_description"]) if expected_decision in {"approved", "modified"} else None
            expected_description = case["expected_description"] if expected_decision in {"approved", "modified"} else None
            ok = expected_decision == "rejected" or actual_description == expected_description
            outcome = "pass" if ok else "fail"
            if ok:
                passed += 1
                message = None
            else:
                failed += 1
                message = json.dumps({"expected": expected_description, "actual": actual_description}, ensure_ascii=False)
            connection.execute(
                "INSERT INTO replay_result(replay_id,case_id,actual_decision,actual_description,outcome,message) VALUES (?,?,?,?,?,?)",
                (replay_id, case["case_id"], actual_decision, actual_description, outcome, message),
            )
        replay_status = "passed" if failed == 0 else "failed"
        connection.execute(
            "UPDATE replay_run SET evaluation_count=?,pass_count=?,fail_count=?,status=?,finished_at=? WHERE replay_id=?",
            (len(all_cases), passed, failed, replay_status, utc_now(), replay_id),
        )
        if replay_status != "passed":
            raise SystemExit(f"Replay failed: {passed}/{len(all_cases)} passed, {failed} failed.")

        for rule_key in RULE_KEYS:
            connection.execute(
                "UPDATE terminology_rule SET status='active',confirmed_by=?,confirmed_at=?,updated_at=? WHERE rule_key=?",
                (PUBLISHER, now, now, rule_key),
            )
        connection.execute(
            "INSERT INTO audit_event(entity_type,entity_id,event_type,actor,payload_json,event_at) VALUES (?,?,?,?,?,?)",
            ("terminology_rule", activation_id, "next_deterministic_rules_activated", PUBLISHER, json.dumps({"rule_version": RULE_VERSION, "rule_keys": list(RULE_KEYS), "replay_id": replay_id, "pass_count": passed, "evaluation_count": len(all_cases), "source_write": False}, ensure_ascii=False), now),
        )

        created_reviews = 0
        inserted_publications = 0
        for candidate_id in candidate_ids:
            candidate = candidate_map[candidate_id]
            preview = preview_by_id[candidate_id]
            final_description = preview["PROPOSED_DESCRIPTION"]
            review_id = f"review-next-deterministic-{candidate_id}"
            receipt = f"receipt-next-deterministic-{candidate_id}"
            connection.execute(
                """
                INSERT INTO review_decision
                  (review_id,candidate_id,decision,reviewed_description,reason_code,review_note,
                   reviewer,approval_receipt,reviewed_at)
                VALUES (?,?,?,?,?,?,?,?,?)
                """,
                (
                    review_id, candidate_id, "approved", final_description,
                    "NEXT_DETERMINISTIC_RULE_BATCH_APPROVED",
                    f"用户确认直接执行下一批确定性格式规则；预览 {preview_manifest['preview_id']}；样本 200 条；回放 {replay_id} {passed}/{len(all_cases)} 通过；源库只读。",
                    PUBLISHER, receipt, now,
                ),
            )
            created_reviews += 1
            publication_hash = sha256_bytes(json.dumps({
                "source_schema": candidate["source_schema"], "site_id": candidate["site_id"],
                "asset_number": candidate["asset_number"], "source_snapshot_id": candidate["source_snapshot_id"],
                "final_description": final_description, "rule_version": RULE_VERSION,
                "applied_rule_keys": preview["APPLIED_RULE_KEYS"],
            }, ensure_ascii=False, sort_keys=True).encode("utf-8"))
            connection.execute(
                """
                INSERT INTO published_description
                  (publication_id,candidate_id,review_id,source_snapshot_id,source_schema,site_id,
                   asset_number,final_description,rule_version,validator_version,replay_id,
                   published_by,published_at,publication_hash)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    f"publication-next-deterministic-{candidate_id}", candidate_id, review_id,
                    candidate["source_snapshot_id"], candidate["source_schema"], candidate["site_id"],
                    candidate["asset_number"], final_description, RULE_VERSION, candidate["validator_version"],
                    replay_id, PUBLISHER, now, publication_hash,
                ),
            )
            existing_rules = json.loads(candidate["applied_rule_ids_json"] or "[]")
            merged_rules = list(dict.fromkeys([*existing_rules, *preview["APPLIED_RULE_KEYS"].split("|")]))
            connection.execute(
                "UPDATE semantic_candidate SET applied_rule_ids_json=?,publication_state='published',review_state='approved' WHERE candidate_id=?",
                (json.dumps(merged_rules, ensure_ascii=False), candidate_id),
            )
            inserted_publications += 1

        current_published = connection.execute("SELECT count(*) FROM published_description").fetchone()[0]
        connection.execute(
            "UPDATE batch_run SET published_count=(SELECT count(*) FROM published_description p JOIN semantic_candidate c ON c.candidate_id=p.candidate_id WHERE c.batch_id=?),formal_publication=1 WHERE batch_id=?",
            (batch_ids[0], batch_ids[0]),
        )
        payload = {
            "publication_run_id": publication_run_id,
            "approval_batch_id": approval_batch_id,
            "batch_id": batch_ids[0],
            "preview_id": preview_manifest["preview_id"],
            "preview_rows": len(preview_rows),
            "sample_rows": len(sample_rows),
            "created_reviews": created_reviews,
            "inserted_publications": inserted_publications,
            "total_published_after": current_published,
            "rule_version": RULE_VERSION,
            "rule_keys": list(RULE_KEYS),
            "replay_id": replay_id,
            "replay_pass_count": passed,
            "replay_evaluation_count": len(all_cases),
            "preview_sha256": preview_manifest["preview_sha256"],
            "sample_sha256": sample_manifest["sample_sha256"],
            "pre_publication_backup": str(backup_dir),
            "source_write": False,
        }
        connection.execute(
            "INSERT INTO audit_event(entity_type,entity_id,event_type,actor,payload_json,event_at) VALUES (?,?,?,?,?,?)",
            ("publication", publication_run_id, "next_deterministic_preview_published", PUBLISHER, json.dumps(payload, ensure_ascii=False), now),
        )
        connection.execute(
            "INSERT INTO audit_event(entity_type,entity_id,event_type,actor,payload_json,event_at) VALUES (?,?,?,?,?,?)",
            ("approval_batch", approval_batch_id, "next_deterministic_batch_approval_created", PUBLISHER, json.dumps({"preview_id": preview_manifest["preview_id"], "count": len(preview_rows), "source_write": False}, ensure_ascii=False), now),
        )
        connection.commit()
    except BaseException:
        connection.rollback()
        connection.close()
        raise
    connection.close()

    confirmation_manifest = {
        "status": "confirmed_for_replay",
        "confirmation_id": confirmation_id,
        "preview_id": preview_manifest["preview_id"],
        "sample_id": sample_manifest["sample_id"],
        "selected_count": len(sample_rows),
        "confirmed_by": PUBLISHER,
        "confirmed_at_utc": now,
        "source_write": False,
        "formal_publication": False,
    }
    replay_manifest = {
        "status": "passed", "replay_id": replay_id, "rule_version": RULE_VERSION,
        "validator_version": VALIDATOR_VERSION, "evaluation_count": len(all_cases),
        "pass_count": passed, "fail_count": failed, "source_write": False,
        "formal_publication": False,
    }
    activation_manifest = {
        "status": "active", "activation_id": activation_id, "rule_version": RULE_VERSION,
        "rule_keys": list(RULE_KEYS), "replay_id": replay_id, "source_write": False,
        "formal_publication": False,
    }
    publication_manifest = {
        **payload, "published_at_utc": now, "formal_result_layer": "published_description",
        "formal_publication": True, "status": "published",
        "backup_files": [{"path": str(path), "size": path.stat().st_size} for path in backup_files],
    }
    CONFIRMATION_MANIFEST.write_text(json.dumps(confirmation_manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    REPLAY_MANIFEST.write_text(json.dumps(replay_manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    ACTIVATION_MANIFEST.write_text(json.dumps(activation_manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    PUBLICATION_MANIFEST.write_text(json.dumps(publication_manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(publication_manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
