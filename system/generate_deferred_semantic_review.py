"""Create a read-only review package for ambiguous candidate rewrites.

The current high-quality pending population has no new confirmed
deterministic rule. This package exposes the existing candidate-vs-source
differences with context for AI-assisted or human review, without changing
candidate state or the formal result layer.
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
OUTPUT_DIR = PROJECT_ROOT / "pilots" / "HD_SAAS" / "deferred_semantic_review"
REVIEW_CSV = OUTPUT_DIR / "candidate_rewrite_review.csv"
SAMPLE_CSV = OUTPUT_DIR / "sample_200.csv"
SUMMARY_JSON = OUTPUT_DIR / "summary.json"
MANIFEST_JSON = OUTPUT_DIR / "manifest.json"
SAMPLE_MANIFEST_JSON = OUTPUT_DIR / "sample_manifest.json"
SAMPLE_TARGET = 200


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def select_sample(rows: list[dict[str, str]], target: int) -> list[dict[str, str]]:
    strata: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        strata[f"{row['SITEID']} / {row['DIFF_CLASS']}"] .append(row)
    for bucket in strata.values():
        bucket.sort(key=lambda row: (row["ASSETNUM"], row["CANDIDATE_ID"]))
    selected: list[dict[str, str]] = []
    keys = sorted(strata)
    for key in keys:
        if strata[key] and len(selected) < target:
            selected.append(strata[key].pop(0))
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
          AND c.original_description<>c.candidate_description
        ORDER BY d.site_id,d.asset_number,c.candidate_id
        """
    ).fetchall()
    connection.close()

    columns = [
        "SITEID", "ASSETNUM", "ASSETID", "CANDIDATE_ID", "BATCH_ID",
        "SOURCE_SNAPSHOT_ID", "SOURCE_SCHEMA", "SOURCE_ROW_HASH", "CONTEXT_HASH",
        "ORIGINAL_DESCRIPTION", "EXISTING_CANDIDATE_DESCRIPTION", "DIFF_CLASS",
        "SEMANTIC_ACTION", "REASON_CODES", "LOCATION_CODE", "LOCATION_DESCRIPTION",
        "LOCATION_PARENT", "CLASSSTRUCTURE_DESCRIPTION", "CLASSIFICATION_DESCRIPTION",
        "CONFIDENCE", "VALIDATOR_STATUS", "REVIEW_STATE", "PUBLICATION_STATE",
        "RULE_VERSION", "VALIDATOR_VERSION", "REVIEW_STATUS",
    ]
    output_rows: list[dict[str, str]] = []
    by_class: Counter[str] = Counter()
    by_site: Counter[str] = Counter()
    by_class_site: dict[str, Counter[str]] = defaultdict(Counter)
    for row in rows:
        original = row["original_description"] or ""
        candidate = row["candidate_description"] or ""
        if original != candidate and original.encode("utf-8") and candidate:
            if __import__("unicodedata").normalize("NFKC", original) == candidate:
                diff_class = "unicode_nfkc_or_width_change_requires_semantic_check"
            else:
                diff_class = "other_candidate_rewrite_requires_semantic_check"
        else:
            continue
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
            "DIFF_CLASS": diff_class,
            "SEMANTIC_ACTION": row["semantic_action"] or "",
            "REASON_CODES": row["reason_codes_json"] or "[]",
            "LOCATION_CODE": row["location_code"] or "",
            "LOCATION_DESCRIPTION": row["location_description"] or "",
            "LOCATION_PARENT": row["location_parent"] or "",
            "CLASSSTRUCTURE_DESCRIPTION": row["class_structure_description"] or "",
            "CLASSIFICATION_DESCRIPTION": row["classification_description"] or "",
            "CONFIDENCE": row["confidence"],
            "VALIDATOR_STATUS": row["validator_status"],
            "REVIEW_STATE": row["review_state"],
            "PUBLICATION_STATE": row["publication_state"],
            "RULE_VERSION": row["rule_version"],
            "VALIDATOR_VERSION": row["validator_version"],
            "REVIEW_STATUS": "deferred_no_confirmed_semantic_rule",
        }
        output_rows.append(item)
        by_class[diff_class] += 1
        by_site[item["SITEID"]] += 1
        by_class_site[diff_class][item["SITEID"]] += 1

    with REVIEW_CSV.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(output_rows)
    sample_rows = select_sample(output_rows, min(SAMPLE_TARGET, len(output_rows)))
    with SAMPLE_CSV.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(sample_rows)

    summary = {
        "pending_high_quality_scope": 0,
        "changed_candidate_rows": len(output_rows),
        "sample_rows": len(sample_rows),
        "by_diff_class": dict(sorted(by_class.items())),
        "by_site": dict(sorted(by_site.items())),
        "by_diff_class_site": {key: dict(sorted(value.items())) for key, value in sorted(by_class_site.items())},
        "reason_code": "NO_CONFIRMED_SEMANTIC_REWRITE",
        "review_status": "deferred_no_confirmed_semantic_rule",
        "source_write": False,
        "formal_publication": False,
    }
    connection = connect_readonly(DB)
    summary["pending_high_quality_scope"] = connection.execute(
        """
        SELECT count(*) FROM semantic_candidate c JOIN device_identity d ON d.device_id=c.device_id
        WHERE c.publication_state='unpublished' AND c.review_state='pending'
          AND c.validator_status='candidate' AND c.confidence='high' AND trim(d.original_description)<>''
        """
    ).fetchone()[0]
    connection.close()
    generated_at = utc_now()
    manifest = {
        "review_package_id": f"deferred-semantic-review-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}",
        "generated_at_utc": generated_at,
        "source_database": str(DB),
        "source_scope": "unpublished + pending + candidate + high confidence + non-empty description + original != candidate",
        "changed_candidate_rows": len(output_rows),
        "sample_rows": len(sample_rows),
        "review_file": str(REVIEW_CSV),
        "sample_file": str(SAMPLE_CSV),
        "review_sha256": sha256(REVIEW_CSV),
        "sample_sha256": sha256(SAMPLE_CSV),
        "rule_status": "no_new_confirmed_deterministic_rule",
        "source_write": False,
        "formal_publication": False,
        "approval_gate": "AI or human review must confirm the semantic meaning before any rule promotion or publication",
        "summary": summary,
    }
    sample_manifest = {
        "sample_id": f"deferred-semantic-sample-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}",
        "target_count": SAMPLE_TARGET,
        "selected_count": len(sample_rows),
        "strategy": "round_robin_by_SITEID_and_DIFF_CLASS_then_ASSETNUM",
        "status": "open_for_ai_or_human_review",
        "source_write": False,
        "formal_publication": False,
        "sample_file": str(SAMPLE_CSV),
        "sample_sha256": manifest["sample_sha256"],
        "review_package_id": manifest["review_package_id"],
    }
    SUMMARY_JSON.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    MANIFEST_JSON.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    SAMPLE_MANIFEST_JSON.write_text(json.dumps(sample_manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"manifest": str(MANIFEST_JSON), "sample_manifest": str(SAMPLE_MANIFEST_JSON), **summary}, ensure_ascii=False))


if __name__ == "__main__":
    main()
