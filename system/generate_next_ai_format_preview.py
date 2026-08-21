"""Generate read-only previews for the two AI-confirmed format clusters.

The safe punctuation cluster is a formal rewrite preview. The signed/negative
value cluster is a preserve-original result and is deliberately excluded from
publication. Neither output mutates candidate, review, or source tables.
"""
from __future__ import annotations

import csv
import hashlib
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from pipeline.contracts import connect_readonly

ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parent
DB = ROOT / "data" / "semantic_workflow.sqlite3"
INPUT_CSV = PROJECT_ROOT / "pilots" / "HD_SAAS" / "next_ai_review" / "sample_200_judgment.csv"
OUTPUT_DIR = PROJECT_ROOT / "pilots" / "HD_SAAS" / "next_ai_format_preview"
PREVIEW_CSV = OUTPUT_DIR / "safe_punctuation_formal_preview.csv"
PRESERVE_CSV = OUTPUT_DIR / "signed_value_preserve_result.csv"
SUMMARY_JSON = OUTPUT_DIR / "summary.json"
MANIFEST_JSON = OUTPUT_DIR / "manifest.json"
RULE_VERSION = "ai-confirmed-format-preview-20260812-v1"

SAFE_MAPPINGS = {"\ufe51": "\u3001", "\uff1b": ";", "\uff1c": "<", "\uff1e": ">"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_transform(value: str) -> str:
    return "".join(SAFE_MAPPINGS.get(char, char) for char in value)


def safe_rule_keys(original: str, candidate: str) -> list[str]:
    if original == candidate or safe_transform(original) != candidate or len(original) != len(candidate):
        return []
    return [
        "format.confirmed_safe_punctuation_shape"
        for original_char, candidate_char in zip(original, candidate)
        if original_char != candidate_char
        and original_char in SAFE_MAPPINGS
        and SAFE_MAPPINGS[original_char] == candidate_char
    ] or []


def diff_category(original: str, candidate: str) -> str:
    if "-" in original and "-" not in candidate:
        return "minus_or_signed_value_deleted"
    if original != candidate and safe_rule_keys(original, candidate):
        return "safe_punctuation_shape"
    return "other"


def base_item(row: sqlite3.Row) -> dict[str, str]:
    return {
        "SITEID": row["site_id"] or "",
        "ASSETNUM": row["asset_number"] or "",
        "ASSETID": row["source_asset_id"] or "",
        "CANDIDATE_ID": row["candidate_id"],
        "BATCH_ID": row["batch_id"],
        "SOURCE_SNAPSHOT_ID": row["source_snapshot_id"],
        "SOURCE_SCHEMA": row["source_schema"] or "",
        "SOURCE_ROW_HASH": row["source_row_hash"] or "",
        "CONTEXT_HASH": row["context_hash"] or "",
        "ORIGINAL_DESCRIPTION": row["original_description"] or "",
        "EXISTING_CANDIDATE_DESCRIPTION": row["candidate_description"] or "",
        "LOCATION_CODE": row["location_code"] or "",
        "LOCATION_DESCRIPTION": row["location_description"] or "",
        "LOCATION_PARENT": row["location_parent"] or "",
        "CLASSSTRUCTURE_DESCRIPTION": row["class_structure_description"] or "",
        "CLASSIFICATION_DESCRIPTION": row["classification_description"] or "",
        "REVIEW_STATE": row["review_state"],
        "PUBLICATION_STATE": row["publication_state"],
        "VALIDATOR_STATUS": row["validator_status"],
        "CONFIDENCE": row["confidence"],
    }


def write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    if not rows:
        raise SystemExit(f"No rows to write: {path}")
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with INPUT_CSV.open(encoding="utf-8-sig", newline="") as handle:
        sample_rows = list(csv.DictReader(handle))
    if len(sample_rows) != 200 or len({row["CANDIDATE_ID"] for row in sample_rows}) != 200:
        raise SystemExit("The AI sample must contain 200 unique rows.")
    sample_accept = [row for row in sample_rows if row["AI_DECISION"] == "接受候选"]
    sample_preserve = [row for row in sample_rows if row["AI_DECISION"] == "保留原文"]
    if len(sample_accept) != 13 or any(row["DIFF_CATEGORY"] != "safe_punctuation_shape" for row in sample_accept):
        raise SystemExit("The accepted AI sample gate is not exactly 13 safe punctuation rows.")
    if len(sample_preserve) != 21 or any(row["DIFF_CATEGORY"] != "minus_or_signed_value_deleted" for row in sample_preserve):
        raise SystemExit("The preserve-original AI sample gate is not exactly 21 signed-value rows.")

    connection = connect_readonly(DB)
    rows = connection.execute(
        """
        SELECT c.candidate_id,c.batch_id,c.original_description,c.candidate_description,
          c.review_state,c.publication_state,c.validator_status,c.confidence,
          d.source_snapshot_id,d.source_schema,d.source_asset_id,d.site_id,d.asset_number,
          d.source_row_hash,d.context_hash,d.location_code,d.location_description,d.location_parent,
          d.class_structure_description,d.classification_description
        FROM semantic_candidate c JOIN device_identity d ON d.device_id=c.device_id
        WHERE c.publication_state='unpublished' AND c.review_state='pending'
          AND c.validator_status='candidate' AND c.confidence='high'
          AND trim(d.original_description)<>''
          AND c.original_description<>c.candidate_description
        ORDER BY d.site_id,d.asset_number,c.candidate_id
        """
    ).fetchall()
    accepted_ids = {row["candidate_id"] for row in connection.execute("SELECT candidate_id FROM ai_review_decision WHERE sample_id=(SELECT sample_id FROM ai_review_decision ORDER BY reviewed_at LIMIT 1) AND decision='accept_candidate'").fetchall()}
    preserved_ids = {row["candidate_id"] for row in connection.execute("SELECT candidate_id FROM ai_review_decision WHERE sample_id=(SELECT sample_id FROM ai_review_decision ORDER BY reviewed_at LIMIT 1) AND decision='keep_original'").fetchall()}
    connection.close()

    safe_rows: list[dict[str, str]] = []
    preserve_rows: list[dict[str, str]] = []
    for row in rows:
        original = row["original_description"] or ""
        candidate = row["candidate_description"] or ""
        category = diff_category(original, candidate)
        if category == "safe_punctuation_shape":
            item = base_item(row)
            item.update({
                "PROPOSED_DESCRIPTION": candidate,
                "DIFF_CATEGORY": category,
                "APPLIED_RULE_KEYS": "|".join(sorted(set(safe_rule_keys(original, candidate)))),
                "PREVIEW_STATUS": "ready_for_replay_not_published",
                "SOURCE_WRITE": "false",
                "FORMAL_PUBLICATION": "false",
            })
            safe_rows.append(item)
        elif category == "minus_or_signed_value_deleted":
            item = base_item(row)
            item.update({
                "PROPOSED_DESCRIPTION": original,
                "DIFF_CATEGORY": category,
                "APPLIED_RULE_KEYS": "guardrail.preserve_signed_or_negative_value",
                "RESULT_ACTION": "preserve_original",
                "PREVIEW_STATUS": "preserve_only_not_published",
                "SOURCE_WRITE": "false",
                "FORMAL_PUBLICATION": "false",
            })
            preserve_rows.append(item)

    if len(safe_rows) != 66:
        raise SystemExit(f"Expected 66 safe punctuation rows, found {len(safe_rows)}")
    if len(preserve_rows) != 65:
        raise SystemExit(f"Expected 65 signed-value rows, found {len(preserve_rows)}")
    if not accepted_ids or not preserved_ids or not accepted_ids.issubset({row["CANDIDATE_ID"] for row in safe_rows}) or not preserved_ids.issubset({row["CANDIDATE_ID"] for row in preserve_rows}):
        raise SystemExit("The confirmed AI sample IDs are not contained in the full preview clusters.")

    write_csv(PREVIEW_CSV, safe_rows)
    write_csv(PRESERVE_CSV, preserve_rows)
    manifest = {
        "preview_id": f"ai-format-preview-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}",
        "generated_at_utc": utc_now(),
        "source_database": str(DB),
        "source_scope": "unpublished + pending + candidate + high confidence + exact confirmed format clusters",
        "rule_version": RULE_VERSION,
        "preview_rows": len(safe_rows),
        "preserve_rows": len(preserve_rows),
        "preview_file": str(PREVIEW_CSV),
        "preserve_file": str(PRESERVE_CSV),
        "preview_sha256": sha256(PREVIEW_CSV),
        "preserve_sha256": sha256(PRESERVE_CSV),
        "sample_id": sample_rows[0].get("SAMPLE_ID", "next-ai-review-20260812T085842Z"),
        "sample_confirmed": {"accept_candidate": len(accepted_ids), "keep_original": len(preserved_ids), "needs_review": 166},
        "by_site": dict(sorted(Counter(row["SITEID"] for row in safe_rows).items())),
        "preserve_by_site": dict(sorted(Counter(row["SITEID"] for row in preserve_rows).items())),
        "source_write": False,
        "formal_publication": False,
        "status": "preview_ready_replay_required",
    }
    summary = {**manifest, "preview_sha256": manifest["preview_sha256"], "preserve_sha256": manifest["preserve_sha256"]}
    SUMMARY_JSON.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    MANIFEST_JSON.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"manifest": str(MANIFEST_JSON), "preview_rows": 66, "preserve_rows": 65, "source_write": False, "formal_publication": False}, ensure_ascii=False))


if __name__ == "__main__":
    main()
