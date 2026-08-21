"""Generate a read-only preview for pure Unicode NFKC changes.

Only rows whose current candidate is exactly ``unicodedata.normalize('NFKC',
original)`` are included. Rows with any other transformation are excluded and
remain available for a separate semantic review queue.
"""
from __future__ import annotations

import csv
import hashlib
import json
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from pipeline.contracts import connect_readonly


ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parent
DB = ROOT / "data" / "semantic_workflow.sqlite3"
OUTPUT_DIR = PROJECT_ROOT / "pilots" / "HD_SAAS" / "nfkc_safe_format_preview"
PREVIEW_CSV = OUTPUT_DIR / "rewrite_preview.csv"
SAMPLE_CSV = OUTPUT_DIR / "sample_200.csv"
MANIFEST_JSON = OUTPUT_DIR / "manifest.json"
SAMPLE_MANIFEST_JSON = OUTPUT_DIR / "sample_manifest.json"
EXCLUDED_JSON = OUTPUT_DIR / "excluded_non_nfkc_changes.json"
RULE_VERSION = "nfkc-format-proposed-20260812-v1"
VALIDATOR_VERSION = "hd-semantic-validator-0.2.0"
SAMPLE_TARGET = 200


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def changed_codepoints(original: str, proposed: str) -> str:
    pairs: list[str] = []
    for source, target in zip(original, proposed):
        if source != target:
            pairs.append(
                f"U+{ord(source):04X}({unicodedata.name(source, 'UNKNOWN')})>"
                f"U+{ord(target):04X}({unicodedata.name(target, 'UNKNOWN')})"
            )
    return "|".join(dict.fromkeys(pairs))


def mapping_group(original: str, proposed: str) -> str:
    pairs = {(source, target) for source, target in zip(original, proposed) if source != target}
    if any(source in "ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ" for source, _ in pairs):
        return "roman_numeral_to_ascii"
    if any(source in "①②③④⑤⑥⑦⑧⑨⑩" for source, _ in pairs):
        return "circled_digit_to_ascii"
    if any("FULLWIDTH" in unicodedata.name(source, "") for source, _ in pairs):
        return "fullwidth_ascii_to_ascii"
    if any(unicodedata.category(source).startswith("P") for source, _ in pairs):
        return "compatibility_punctuation_to_ascii"
    return "other_nfkc"


def select_sample(rows: list[dict[str, str]], target: int) -> list[dict[str, str]]:
    buckets: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        buckets[f"{row['SITEID']}|{row['MAPPING_GROUP']}"] .append(row)
    for bucket in buckets.values():
        bucket.sort(key=lambda row: (row["ASSETNUM"], row["CANDIDATE_ID"]))

    selected: list[dict[str, str]] = []
    keys = sorted(buckets)
    while len(selected) < target:
        progressed = False
        for key in keys:
            if buckets[key]:
                selected.append(buckets[key].pop(0))
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
        SELECT c.candidate_id,c.batch_id,c.review_state,c.publication_state,
          c.validator_status,c.confidence,c.original_description,c.candidate_description,
          c.rule_version,c.validator_version,c.reason_codes_json,c.applied_rule_ids_json,
          d.source_snapshot_id,d.source_schema,d.source_asset_id,d.site_id,d.asset_number,
          d.source_row_hash,d.context_hash,d.location_code,d.location_description,d.location_parent,
          d.class_structure_description,d.classification_description
        FROM semantic_candidate c
        JOIN device_identity d ON d.device_id=c.device_id
        WHERE c.publication_state='unpublished'
          AND c.review_state='pending'
          AND c.validator_status='candidate'
          AND c.confidence='high'
          AND c.original_description<>c.candidate_description
        ORDER BY d.site_id,d.asset_number,c.candidate_id
        """
    ).fetchall()
    connection.close()

    columns = [
        "SITEID", "ASSETNUM", "ASSETID", "CANDIDATE_ID", "BATCH_ID",
        "SOURCE_SNAPSHOT_ID", "SOURCE_SCHEMA", "SOURCE_ROW_HASH", "CONTEXT_HASH",
        "ORIGINAL_DESCRIPTION", "EXISTING_CANDIDATE_DESCRIPTION", "PROPOSED_DESCRIPTION",
        "MAPPING_GROUP", "CHANGED_CODEPOINTS", "LOCATION_CODE", "LOCATION_DESCRIPTION",
        "LOCATION_PARENT", "CLASSSTRUCTURE_DESCRIPTION", "CLASSIFICATION_DESCRIPTION",
        "REVIEW_STATE", "PUBLICATION_STATE", "VALIDATOR_STATUS", "CONFIDENCE",
        "PREVIEW_STATUS", "SOURCE_WRITE", "FORMAL_PUBLICATION",
    ]
    preview_rows: list[dict[str, str]] = []
    excluded: list[dict[str, str]] = []
    by_site: Counter[str] = Counter()
    by_group: Counter[str] = Counter()
    for row in rows:
        original = row["original_description"] or ""
        candidate = row["candidate_description"] or ""
        proposed = unicodedata.normalize("NFKC", original)
        if any(mark in original or mark in candidate or mark in proposed for mark in ("?", "？")):
            excluded.append(
                {
                    "candidate_id": row["candidate_id"],
                    "site_id": row["site_id"],
                    "asset_number": row["asset_number"],
                    "original_description": original,
                    "existing_candidate_description": candidate,
                    "reason": "question_mark_context_deferred",
                }
            )
            continue
        if proposed != candidate or proposed == original:
            excluded.append(
                {
                    "candidate_id": row["candidate_id"],
                    "site_id": row["site_id"],
                    "asset_number": row["asset_number"],
                    "original_description": original,
                    "existing_candidate_description": candidate,
                    "reason": "not_pure_nfkc_or_unchanged",
                }
            )
            continue
        group = mapping_group(original, proposed)
        item = {
            "SITEID": row["site_id"] or "",
            "ASSETNUM": row["asset_number"] or "",
            "ASSETID": row["source_asset_id"] or "",
            "CANDIDATE_ID": row["candidate_id"],
            "BATCH_ID": row["batch_id"],
            "SOURCE_SNAPSHOT_ID": row["source_snapshot_id"],
            "SOURCE_SCHEMA": row["source_schema"] or "",
            "SOURCE_ROW_HASH": row["source_row_hash"] or "",
            "CONTEXT_HASH": row["context_hash"] or "",
            "ORIGINAL_DESCRIPTION": original,
            "EXISTING_CANDIDATE_DESCRIPTION": candidate,
            "PROPOSED_DESCRIPTION": proposed,
            "MAPPING_GROUP": group,
            "CHANGED_CODEPOINTS": changed_codepoints(original, proposed),
            "LOCATION_CODE": row["location_code"] or "",
            "LOCATION_DESCRIPTION": row["location_description"] or "",
            "LOCATION_PARENT": row["location_parent"] or "",
            "CLASSSTRUCTURE_DESCRIPTION": row["class_structure_description"] or "",
            "CLASSIFICATION_DESCRIPTION": row["classification_description"] or "",
            "REVIEW_STATE": row["review_state"],
            "PUBLICATION_STATE": row["publication_state"],
            "VALIDATOR_STATUS": row["validator_status"],
            "CONFIDENCE": row["confidence"],
            "PREVIEW_STATUS": "proposed_not_active",
            "SOURCE_WRITE": "false",
            "FORMAL_PUBLICATION": "false",
        }
        preview_rows.append(item)
        by_site[item["SITEID"]] += 1
        by_group[group] += 1

    if len(preview_rows) != 4306:
        raise SystemExit(f"Expected 4306 pure-NFKC rows after question-mark exclusion, got {len(preview_rows)}.")
    if len({row["CANDIDATE_ID"] for row in preview_rows}) != len(preview_rows):
        raise SystemExit("Duplicate candidate IDs in NFKC preview.")
    if any(row["REVIEW_STATE"] != "pending" or row["PUBLICATION_STATE"] != "unpublished" for row in preview_rows):
        raise SystemExit("NFKC preview contains a non-pending or already published row.")

    with PREVIEW_CSV.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(preview_rows)
    sample_rows = select_sample(preview_rows, SAMPLE_TARGET)
    with SAMPLE_CSV.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(sample_rows)

    generated_at = utc_now()
    manifest = {
        "preview_id": f"nfkc-format-preview-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}",
        "generated_at_utc": generated_at,
        "source_database": str(DB),
        "source_scope": "unpublished + pending + candidate + high confidence + pure NFKC-only change",
        "preview_rows": len(preview_rows),
        "sample_rows": len(sample_rows),
        "excluded_non_nfkc_changes": len(excluded),
        "excluded_question_mark_context": sum(item["reason"] == "question_mark_context_deferred" for item in excluded),
        "by_site": dict(sorted(by_site.items())),
        "by_mapping_group": dict(sorted(by_group.items())),
        "preview_file": str(PREVIEW_CSV),
        "sample_file": str(SAMPLE_CSV),
        "preview_sha256": sha256_file(PREVIEW_CSV),
        "sample_sha256": sha256_file(SAMPLE_CSV),
        "rule_version": RULE_VERSION,
        "validator_version": VALIDATOR_VERSION,
        "rule_status": "proposed_not_active",
        "source_write": False,
        "formal_publication": False,
        "approval_gate": "confirm stratified sample and replay before activation or publication",
    }
    sample_manifest = {
        "sample_id": f"nfkc-format-sample-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}",
        "sample_name": "nfkc-format-sample-200-v1",
        "selected_count": len(sample_rows),
        "strategy": "round_robin_by_SITEID_and_MAPPING_GROUP_then_ASSETNUM",
        "source_preview_id": manifest["preview_id"],
        "sample_file": str(SAMPLE_CSV),
        "sample_sha256": manifest["sample_sha256"],
        "status": "open_for_confirmation",
        "source_write": False,
        "formal_publication": False,
    }
    MANIFEST_JSON.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    SAMPLE_MANIFEST_JSON.write_text(json.dumps(sample_manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    EXCLUDED_JSON.write_text(json.dumps({"count": len(excluded), "rows": excluded}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({**manifest, "sample_manifest": sample_manifest}, ensure_ascii=False))


if __name__ == "__main__":
    main()
