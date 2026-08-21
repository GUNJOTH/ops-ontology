"""Confirm the reviewed 200-row space sample as evaluation cases only."""
from __future__ import annotations

import csv
import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parent
DB = ROOT / "data" / "semantic_workflow.sqlite3"
PREVIEW_DIR = PROJECT_ROOT / "pilots" / "HD_SAAS" / "space_rule_preview"
SAMPLE_CSV = PREVIEW_DIR / "space_rule_sample_200.csv"
SAMPLE_MANIFEST = PREVIEW_DIR / "sample_manifest.json"
CONFIRMATION_MANIFEST = PREVIEW_DIR / "confirmation_manifest.json"
RULE_VERSION = "space-normalization-proposed-20260812-v1"
VALIDATOR_VERSION = "hd-semantic-validator-0.2.0"
CONFIRMER = "local-user-confirmed-space-sample"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def transform(value: str) -> str:
    value = value.replace("\u3000", " ")
    value = re.sub(r" {2,}", " ", value)
    return value.strip()


def non_whitespace(value: str) -> str:
    return re.sub(r"\s", "", value)


def main() -> None:
    sample_manifest = json.loads(SAMPLE_MANIFEST.read_text(encoding="utf-8"))
    if sample_manifest.get("status") not in {"open_for_confirmation", "confirmed_for_replay"}:
        raise SystemExit("Sample is not available for confirmation.")
    if sample_manifest.get("source_write") is not False or sample_manifest.get("formal_publication") is not False:
        raise SystemExit("Sample manifest is not read-only.")
    if int(sample_manifest.get("selected_count", 0)) != 200:
        raise SystemExit("The confirmed sample must contain exactly 200 rows.")
    if not SAMPLE_CSV.exists() or sha256(SAMPLE_CSV) != sample_manifest.get("sample_sha256"):
        raise SystemExit("Sample CSV hash does not match its manifest.")

    with SAMPLE_CSV.open(encoding="utf-8-sig", newline="") as handle:
        sample_rows = list(csv.DictReader(handle))
    if len(sample_rows) != 200:
        raise SystemExit(f"Sample CSV has {len(sample_rows)} rows, expected 200.")
    candidate_ids = [row["CANDIDATE_ID"] for row in sample_rows]
    if len(set(candidate_ids)) != len(candidate_ids):
        raise SystemExit("Sample contains duplicate candidate IDs.")

    connection = sqlite3.connect(DB, timeout=60)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=60000")
    connection.execute("PRAGMA foreign_keys=ON")
    placeholders = ",".join("?" for _ in candidate_ids)
    db_rows = connection.execute(
        f"""
        SELECT c.candidate_id,c.batch_id,c.original_description,c.review_state,c.publication_state,
          c.validator_status,c.confidence,c.rule_version,c.validator_version,
          d.source_snapshot_id,d.source_schema,d.site_id,d.asset_number,
          d.location_code,d.location_description,d.location_parent,d.classification_description
        FROM semantic_candidate c JOIN device_identity d ON d.device_id=c.device_id
        WHERE c.candidate_id IN ({placeholders})
        """,
        candidate_ids,
    ).fetchall()
    db_map = {row["candidate_id"]: row for row in db_rows}
    if len(db_map) != len(candidate_ids):
        raise SystemExit(f"Database identity mismatch: {len(db_map)} of {len(candidate_ids)} found.")

    invalid: list[str] = []
    evaluation_rows: list[tuple[object, ...]] = []
    now = utc_now()
    sample_id = sample_manifest["sample_id"]
    confirmation_id = f"space-confirm-{sample_id}"
    for row in sample_rows:
        db_row = db_map[row["CANDIDATE_ID"]]
        proposed = row["PROPOSED_DESCRIPTION"]
        if row["BATCH_ID"] != db_row["batch_id"] or row["SITEID"] != db_row["site_id"] or row["ASSETNUM"] != db_row["asset_number"]:
            invalid.append(f"identity_mismatch:{row['CANDIDATE_ID']}")
        if row["ORIGINAL_DESCRIPTION"] != db_row["original_description"]:
            invalid.append(f"source_description_changed:{row['CANDIDATE_ID']}")
        if db_row["review_state"] != "pending" or db_row["publication_state"] != "unpublished":
            invalid.append(f"state_changed:{row['CANDIDATE_ID']}")
        if db_row["validator_status"] == "blocked":
            invalid.append(f"blocked_candidate:{row['CANDIDATE_ID']}")
        if not proposed.strip() or non_whitespace(row["ORIGINAL_DESCRIPTION"]) != non_whitespace(proposed):
            invalid.append(f"semantic_text_changed:{row['CANDIDATE_ID']}")
        if transform(row["ORIGINAL_DESCRIPTION"]) != proposed:
            invalid.append(f"transform_mismatch:{row['CANDIDATE_ID']}")
        context = {
            "sample_id": sample_id,
            "preview_status": row["PREVIEW_STATUS"],
            "space_pattern": row["SPACE_PATTERN"],
            "applied_rule_keys": row["APPLIED_RULE_KEYS"].split("|") if row["APPLIED_RULE_KEYS"] else [],
            "location_code": row["LOCATION_CODE"],
            "location_description": row["LOCATION_DESCRIPTION"],
            "location_parent": row["LOCATION_PARENT"],
            "classification_description": row["CLASSIFICATION_DESCRIPTION"],
            "source_row_hash": row["SOURCE_ROW_HASH"],
            "context_hash": row["CONTEXT_HASH"],
        }
        case_id = f"case-space-{row['CANDIDATE_ID']}"
        evaluation_rows.append((
            case_id,
            None,
            db_row["source_snapshot_id"],
            db_row["source_schema"],
            db_row["site_id"],
            db_row["asset_number"],
            row["ORIGINAL_DESCRIPTION"],
            json.dumps(context, ensure_ascii=False),
            "approved",
            proposed,
            "space_rule_confirmation",
            1,
            RULE_VERSION,
            now,
        ))
    if invalid:
        connection.close()
        raise SystemExit(json.dumps({"status": "BLOCKED", "invalid_count": len(invalid), "invalid": invalid[:20]}, ensure_ascii=False))

    connection.execute("BEGIN IMMEDIATE")
    for values in evaluation_rows:
        existing = connection.execute("SELECT * FROM evaluation_case WHERE case_id=?", (values[0],)).fetchone()
        if existing is not None:
            if existing["expected_description"] != values[9] or existing["introduced_rule_version"] != RULE_VERSION:
                connection.rollback()
                connection.close()
                raise SystemExit(f"Existing evaluation case conflicts: {values[0]}")
            continue
        connection.execute(
            """
            INSERT INTO evaluation_case
              (case_id,source_review_id,source_snapshot_id,source_schema,site_id,asset_number,
               input_description,context_json,expected_decision,expected_description,failure_type,
               active,introduced_rule_version,created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            values,
        )

    payload = {
        "sample_id": sample_id,
        "confirmation_id": confirmation_id,
        "selected_count": len(evaluation_rows),
        "rule_version": RULE_VERSION,
        "validator_version": VALIDATOR_VERSION,
        "source_write": False,
        "formal_publication": False,
        "confirmed_by": CONFIRMER,
        "confirmed_at": now,
        "evaluation_case_prefix": "case-space-",
    }
    already_audited = connection.execute(
        "SELECT 1 FROM audit_event WHERE entity_type='space_sample' AND entity_id=? AND event_type='space_sample_confirmed'",
        (confirmation_id,),
    ).fetchone()
    if already_audited is None:
        connection.execute(
            "INSERT INTO audit_event(entity_type,entity_id,event_type,actor,payload_json,event_at) VALUES (?,?,?,?,?,?)",
            ("space_sample", confirmation_id, "space_sample_confirmed", CONFIRMER, json.dumps(payload, ensure_ascii=False), now),
        )
    connection.commit()
    active_count = connection.execute("SELECT count(*) FROM evaluation_case WHERE active=1").fetchone()[0]
    connection.close()

    confirmation_manifest = {
        **sample_manifest,
        "status": "confirmed_for_replay",
        "confirmation_id": confirmation_id,
        "confirmed_by": CONFIRMER,
        "confirmed_at_utc": now,
        "evaluation_cases_created_or_verified": len(evaluation_rows),
        "active_evaluation_case_count_after_confirmation": active_count,
        "rule_version": RULE_VERSION,
        "validator_version": VALIDATOR_VERSION,
        "source_write": False,
        "formal_publication": False,
    }
    CONFIRMATION_MANIFEST.write_text(json.dumps(confirmation_manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"status": "confirmed_for_replay", **payload, "active_evaluation_case_count": active_count}, ensure_ascii=False))


if __name__ == "__main__":
    main()
