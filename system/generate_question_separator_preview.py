"""Generate a read-only preview for question-mark separator normalization.

The source MaxiEAM snapshot and the semantic workflow state are never updated
by this script.  A full-width question mark is treated as a separator only
when it is interior to the description; terminal question marks remain out of
scope because they indicate an incomplete description.
"""
from __future__ import annotations

import csv
import hashlib
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from pipeline.contracts import connect_readonly


ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parent
DB = ROOT / "data" / "semantic_workflow.sqlite3"
OUTPUT_DIR = PROJECT_ROOT / "pilots" / "HD_SAAS" / "question_separator_preview"
PREVIEW_CSV = OUTPUT_DIR / "rewrite_preview.csv"
SAMPLE_CSV = OUTPUT_DIR / "sample_200.csv"
MANIFEST_JSON = OUTPUT_DIR / "manifest.json"
SUMMARY_JSON = OUTPUT_DIR / "summary.json"

RULE_VERSION = "question-separator-proposed-20260813-v1"
RULE_KEY = "semantic.separator.fullwidth_question_mark_to_space"
SAMPLE_TARGET = 200
FULLWIDTH_QUESTION = "\uff1f"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def question_positions(value: str) -> list[int]:
    return [index for index, char in enumerate(value) if char == FULLWIDTH_QUESTION]


def classify(value: str) -> str | None:
    positions = question_positions(value)
    if not positions:
        return None
    if any(position == 0 or position == len(value) - 1 for position in positions):
        return None
    return "interior_multiple" if len(positions) > 1 else "interior_single"


def select_sample(rows: list[dict[str, str]], target: int) -> list[dict[str, str]]:
    strata: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        strata[f"{row['SITEID']} / {row['QUESTION_MARK_CLASS']}"] .append(row)
    for bucket in strata.values():
        bucket.sort(key=lambda row: (row["ASSETNUM"], row["CANDIDATE_ID"]))

    selected: list[dict[str, str]] = []
    keys = sorted(strata)
    while len(selected) < target:
        progressed = False
        for key in keys:
            if strata[key]:
                selected.append(strata[key].pop(0))
                progressed = True
                if len(selected) == target:
                    break
        if not progressed:
            break
    return selected


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    connection = connect_readonly(DB)
    rows = connection.execute(
        """
        SELECT c.candidate_id,c.batch_id,c.original_description,c.candidate_description,
          c.semantic_action,c.confidence,c.validator_status,c.review_state,c.publication_state,
          c.reason_codes_json,c.rule_version,c.validator_version,
          d.source_snapshot_id,d.source_schema,d.source_asset_id,d.site_id,d.asset_number,
          d.source_row_hash,d.context_hash,d.location_code,d.location_description,d.location_parent,
          d.class_structure_description,d.classification_description
        FROM semantic_candidate c
        JOIN device_identity d ON d.device_id=c.device_id
        WHERE c.publication_state='unpublished'
          AND c.review_state='pending'
          AND c.validator_status='candidate'
          AND c.confidence='high'
          AND trim(d.original_description)<>''
          AND instr(d.original_description, ?) > 0
        ORDER BY d.site_id,d.asset_number,c.candidate_id
        """,
        (FULLWIDTH_QUESTION,),
    ).fetchall()
    connection.close()

    columns = [
        "SITEID", "ASSETNUM", "ASSETID", "CANDIDATE_ID", "BATCH_ID",
        "SOURCE_SNAPSHOT_ID", "SOURCE_SCHEMA", "SOURCE_ROW_HASH", "CONTEXT_HASH",
        "ORIGINAL_DESCRIPTION", "EXISTING_CANDIDATE_DESCRIPTION", "PROPOSED_DESCRIPTION",
        "QUESTION_MARK_CLASS", "QUESTION_MARK_COUNT", "APPLIED_RULE_KEYS",
        "LOCATION_CODE", "LOCATION_DESCRIPTION", "LOCATION_PARENT",
        "CLASSSTRUCTURE_DESCRIPTION", "CLASSIFICATION_DESCRIPTION", "REASON_CODES",
        "CONFIDENCE", "VALIDATOR_STATUS", "REVIEW_STATE", "PUBLICATION_STATE",
        "RULE_VERSION", "VALIDATOR_VERSION", "PREVIEW_STATUS",
    ]
    preview_rows: list[dict[str, str]] = []
    excluded_terminal = 0
    by_class: Counter[str] = Counter()
    by_site: Counter[str] = Counter()
    examples: dict[str, list[dict[str, str]]] = defaultdict(list)

    for row in rows:
        original = row["original_description"] or ""
        category = classify(original)
        if category is None:
            excluded_terminal += 1
            continue
        positions = question_positions(original)
        proposed = original.replace(FULLWIDTH_QUESTION, " ")
        item = {
            "SITEID": row["site_id"] or "",
            "ASSETNUM": row["asset_number"] or "",
            "ASSETID": row["source_asset_id"] or "",
            "CANDIDATE_ID": row["candidate_id"],
            "BATCH_ID": row["batch_id"],
            "SOURCE_SNAPSHOT_ID": row["source_snapshot_id"] or "",
            "SOURCE_SCHEMA": row["source_schema"] or "",
            "SOURCE_ROW_HASH": row["source_row_hash"] or "",
            "CONTEXT_HASH": row["context_hash"] or "",
            "ORIGINAL_DESCRIPTION": original,
            "EXISTING_CANDIDATE_DESCRIPTION": row["candidate_description"] or "",
            "PROPOSED_DESCRIPTION": proposed,
            "QUESTION_MARK_CLASS": category,
            "QUESTION_MARK_COUNT": str(len(positions)),
            "APPLIED_RULE_KEYS": RULE_KEY,
            "LOCATION_CODE": row["location_code"] or "",
            "LOCATION_DESCRIPTION": row["location_description"] or "",
            "LOCATION_PARENT": row["location_parent"] or "",
            "CLASSSTRUCTURE_DESCRIPTION": row["class_structure_description"] or "",
            "CLASSIFICATION_DESCRIPTION": row["classification_description"] or "",
            "REASON_CODES": row["reason_codes_json"] or "[]",
            "CONFIDENCE": row["confidence"] or "",
            "VALIDATOR_STATUS": row["validator_status"] or "",
            "REVIEW_STATE": row["review_state"] or "",
            "PUBLICATION_STATE": row["publication_state"] or "",
            "RULE_VERSION": RULE_VERSION,
            "VALIDATOR_VERSION": row["validator_version"] or "",
            "PREVIEW_STATUS": "ready_for_sample_and_replay",
        }
        preview_rows.append(item)
        by_class[category] += 1
        by_site[item["SITEID"]] += 1
        if len(examples[category]) < 10:
            examples[category].append({
                "site": item["SITEID"],
                "asset": item["ASSETNUM"],
                "original": original,
                "proposed": proposed,
            })

    if not preview_rows:
        raise SystemExit("No pending high-quality interior question-mark candidates found.")
    if len({row["CANDIDATE_ID"] for row in preview_rows}) != len(preview_rows):
        raise SystemExit("Preview contains duplicate candidate IDs.")
    if any(FULLWIDTH_QUESTION in row["PROPOSED_DESCRIPTION"] for row in preview_rows):
        raise SystemExit("A proposed description still contains a full-width question mark.")
    if any(row["REVIEW_STATE"] != "pending" or row["PUBLICATION_STATE"] != "unpublished" for row in preview_rows):
        raise SystemExit("Preview contains a non-pending or already-published candidate.")

    with PREVIEW_CSV.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(preview_rows)
    sample_rows = select_sample(preview_rows, min(SAMPLE_TARGET, len(preview_rows)))
    with SAMPLE_CSV.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(sample_rows)

    summary = {
        "pending_high_quality_scope": len(rows),
        "preview_rows": len(preview_rows),
        "sample_rows": len(sample_rows),
        "excluded_terminal_question_rows": excluded_terminal,
        "by_question_mark_class": dict(sorted(by_class.items())),
        "by_site": dict(sorted(by_site.items())),
        "examples": dict(examples),
        "rule_key": RULE_KEY,
        "rule_status": "proposed_not_active",
        "source_write": False,
        "formal_publication": False,
    }
    manifest = {
        "preview_id": f"question-separator-preview-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}",
        "generated_at_utc": utc_now(),
        "source_database": str(DB),
        "source_scope": "unpublished + pending + candidate + high confidence + non-empty description + interior full-width question mark",
        "preview_rows": len(preview_rows),
        "sample_rows": len(sample_rows),
        "excluded_terminal_question_rows": excluded_terminal,
        "rule_version": RULE_VERSION,
        "rule_key": RULE_KEY,
        "rule_status": "proposed_not_active",
        "preview_file": str(PREVIEW_CSV),
        "sample_file": str(SAMPLE_CSV),
        "preview_sha256": sha256(PREVIEW_CSV),
        "sample_sha256": sha256(SAMPLE_CSV),
        "source_write": False,
        "formal_publication": False,
        "approval_gate": "sample confirmation, full replay, formal approval, then publication",
        "summary": summary,
    }
    SUMMARY_JSON.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    MANIFEST_JSON.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"manifest": str(MANIFEST_JSON), "preview": str(PREVIEW_CSV), **summary}, ensure_ascii=False))


if __name__ == "__main__":
    main()
