"""Generate a read-only preview and stratified sample for description-space cleanup."""
from __future__ import annotations

import csv
import hashlib
import json
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parent
DB = ROOT / "data" / "semantic_workflow.sqlite3"
OUTPUT_DIR = PROJECT_ROOT / "pilots" / "HD_SAAS" / "space_rule_preview"
PREVIEW_CSV = OUTPUT_DIR / "space_rule_rewrite_preview.csv"
SAMPLE_CSV = OUTPUT_DIR / "space_rule_sample_200.csv"
MANIFEST_JSON = OUTPUT_DIR / "manifest.json"
SUMMARY_JSON = OUTPUT_DIR / "summary.json"
SAMPLE_MANIFEST_JSON = OUTPUT_DIR / "sample_manifest.json"
RULE_VERSION = "space-normalization-proposed-20260812-v1"
RULE_KEYS = (
    "format.fullwidth_space_to_ascii",
    "format.collapse_repeated_ascii_space",
    "format.trim_description_space",
)
SAMPLE_TARGET = 200


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def space_pattern(description: str) -> str:
    categories: list[str] = []
    if "\u3000" in description:
        categories.append("fullwidth_space")
    if "  " in description:
        categories.append("repeated_ascii_space")
    if description != description.strip():
        categories.append("leading_or_trailing_space")
    return "+".join(categories) or "other"


def transform(description: str) -> tuple[str, list[str]]:
    result = description
    rules: list[str] = []
    if "\u3000" in result:
        result = result.replace("\u3000", " ")
        rules.append("format.fullwidth_space_to_ascii")
    if "  " in result:
        result = re.sub(r" {2,}", " ", result)
        rules.append("format.collapse_repeated_ascii_space")
    if result != result.strip():
        result = result.strip()
        rules.append("format.trim_description_space")
    return result, rules


def non_whitespace(value: str) -> str:
    return re.sub(r"\s", "", value)


def select_sample(rows: list[dict[str, str]], target: int) -> list[dict[str, str]]:
    strata: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        strata[f"{row['SITEID']} / {row['SPACE_PATTERN']}"] .append(row)
    for bucket in strata.values():
        bucket.sort(key=lambda row: (row["ASSETNUM"], row["CANDIDATE_ID"]))

    selected: list[dict[str, str]] = []
    for key in sorted(strata):
        if strata[key] and len(selected) < target:
            selected.append(strata[key].pop(0))
    ordered = sorted(strata)
    while len(selected) < target:
        progressed = False
        for key in ordered:
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
    connection = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    batch = connection.execute("SELECT * FROM batch_run ORDER BY started_at DESC LIMIT 1").fetchone()
    if batch is None:
        raise SystemExit("No batch found.")
    rows = connection.execute(
        """
        SELECT c.candidate_id,c.batch_id,c.review_state,c.validator_status,c.confidence,
          c.original_description,c.rule_version,c.validator_version,c.reason_codes_json,
          d.source_snapshot_id,d.source_schema,d.source_asset_id,d.site_id,d.asset_number,
          d.source_row_hash,d.context_hash,d.location_code,d.location_description,d.location_parent,
          d.class_structure_description,d.classification_description
        FROM semantic_candidate c
        JOIN device_identity d ON d.device_id=c.device_id
        WHERE c.publication_state='unpublished'
          AND c.review_state='pending'
          AND (c.original_description LIKE ? OR c.original_description LIKE ?)
        ORDER BY d.site_id,d.asset_number,c.candidate_id
        """,
        ("%  %", "%\u3000%"),
    ).fetchall()
    connection.close()
    if not rows:
        raise SystemExit("No unpublished pending space candidates found.")

    columns = [
        "SITEID", "ASSETNUM", "ASSETID", "CANDIDATE_ID", "BATCH_ID", "SOURCE_SNAPSHOT_ID",
        "SOURCE_SCHEMA", "SOURCE_ROW_HASH", "CONTEXT_HASH", "ORIGINAL_DESCRIPTION",
        "PROPOSED_DESCRIPTION", "APPLIED_RULE_KEYS", "SPACE_PATTERN", "LOCATION_CODE",
        "LOCATION_DESCRIPTION", "LOCATION_PARENT", "CLASSSTRUCTURE_DESCRIPTION",
        "CLASSIFICATION_DESCRIPTION", "REVIEW_STATE", "VALIDATOR_STATUS", "CONFIDENCE",
        "PREVIEW_STATUS",
    ]
    preview_rows: list[dict[str, str]] = []
    by_site: Counter[str] = Counter()
    by_pattern: Counter[str] = Counter()
    by_rule: Counter[str] = Counter()
    invalid: list[str] = []
    for row in rows:
        original = row["original_description"] or ""
        proposed, rules = transform(original)
        pattern = space_pattern(original)
        if not rules or proposed == original:
            invalid.append(f"not_changed:{row['candidate_id']}")
        if non_whitespace(original) != non_whitespace(proposed):
            invalid.append(f"non_whitespace_changed:{row['candidate_id']}")
        if not proposed.strip():
            invalid.append(f"empty_proposed:{row['candidate_id']}")
        preview = {
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
            "PROPOSED_DESCRIPTION": proposed,
            "APPLIED_RULE_KEYS": "|".join(rules),
            "SPACE_PATTERN": pattern,
            "LOCATION_CODE": row["location_code"] or "",
            "LOCATION_DESCRIPTION": row["location_description"] or "",
            "LOCATION_PARENT": row["location_parent"] or "",
            "CLASSSTRUCTURE_DESCRIPTION": row["class_structure_description"] or "",
            "CLASSIFICATION_DESCRIPTION": row["classification_description"] or "",
            "REVIEW_STATE": row["review_state"],
            "VALIDATOR_STATUS": row["validator_status"],
            "CONFIDENCE": row["confidence"],
            "PREVIEW_STATUS": "ready_for_confirmation",
        }
        preview_rows.append(preview)
        by_site[preview["SITEID"]] += 1
        by_pattern[pattern] += 1
        for rule_key in rules:
            by_rule[rule_key] += 1

    if invalid:
        raise SystemExit(json.dumps({"status": "BLOCKED", "invalid": invalid[:20], "invalid_count": len(invalid)}, ensure_ascii=False))
    if len({row["CANDIDATE_ID"] for row in preview_rows}) != len(preview_rows):
        raise SystemExit("Duplicate candidate IDs in preview.")
    if any(row["REVIEW_STATE"] != "pending" or row["VALIDATOR_STATUS"] == "blocked" for row in preview_rows):
        raise SystemExit("Preview contains a non-pending or blocked candidate.")

    with PREVIEW_CSV.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(preview_rows)
    sample_rows = select_sample(preview_rows, min(SAMPLE_TARGET, len(preview_rows)))
    with SAMPLE_CSV.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(sample_rows)

    generated_at = utc_now()
    summary = {
        "candidate_rows": len(preview_rows),
        "sample_rows": len(sample_rows),
        "pending_rows": len(preview_rows),
        "by_site": dict(sorted(by_site.items())),
        "by_pattern": dict(sorted(by_pattern.items())),
        "by_rule": dict(sorted(by_rule.items())),
        "sample_by_site": dict(sorted(Counter(row["SITEID"] for row in sample_rows).items())),
        "sample_by_pattern": dict(sorted(Counter(row["SPACE_PATTERN"] for row in sample_rows).items())),
        "examples": [
            {
                "site": row["SITEID"],
                "asset": row["ASSETNUM"],
                "pattern": row["SPACE_PATTERN"],
                "original": row["ORIGINAL_DESCRIPTION"],
                "proposed": row["PROPOSED_DESCRIPTION"],
                "rules": row["APPLIED_RULE_KEYS"],
            }
            for row in preview_rows[:20]
        ],
    }
    manifest = {
        "preview_id": f"space-preview-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}",
        "generated_at_utc": generated_at,
        "batch_id": batch["batch_id"],
        "source_snapshot_id": batch["source_snapshot_id"],
        "source_scope": "semantic_candidate where publication_state=unpublished and review_state=pending",
        "source_write": False,
        "formal_publication": False,
        "rule_version": RULE_VERSION,
        "rule_status": "proposed_not_active",
        "rule_keys": list(RULE_KEYS),
        "preview_file": str(PREVIEW_CSV),
        "sample_file": str(SAMPLE_CSV),
        "preview_rows": len(preview_rows),
        "sample_rows": len(sample_rows),
        "preview_sha256": sha256(PREVIEW_CSV),
        "sample_sha256": sha256(SAMPLE_CSV),
        "summary": summary,
    }
    sample_manifest = {
        "sample_id": f"space-sample-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}",
        "sample_name": "space-normalization-200-v1",
        "target_count": SAMPLE_TARGET,
        "selected_count": len(sample_rows),
        "strategy": "round_robin_by_SITEID_and_SPACE_PATTERN_then_ASSETNUM",
        "source_preview_id": manifest["preview_id"],
        "status": "open_for_confirmation",
        "source_write": False,
        "formal_publication": False,
        "sample_file": str(SAMPLE_CSV),
        "sample_sha256": manifest["sample_sha256"],
        "sample_by_site": summary["sample_by_site"],
        "sample_by_pattern": summary["sample_by_pattern"],
    }
    MANIFEST_JSON.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    SUMMARY_JSON.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    SAMPLE_MANIFEST_JSON.write_text(json.dumps(sample_manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"manifest": str(MANIFEST_JSON), "sample_manifest": str(SAMPLE_MANIFEST_JSON), **summary}, ensure_ascii=False))


if __name__ == "__main__":
    main()
