"""Generate a strict read-only preview for AI-supported format clusters."""
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
OUTPUT_DIR = PROJECT_ROOT / "pilots" / "HD_SAAS" / "ai_cluster_preview"
PREVIEW_CSV = OUTPUT_DIR / "rewrite_preview.csv"
SAMPLE_CSV = OUTPUT_DIR / "sample_200.csv"
MANIFEST_JSON = OUTPUT_DIR / "manifest.json"
SUMMARY_JSON = OUTPUT_DIR / "summary.json"
RULE_VERSION = "ai-cluster-format-proposed-20260812-v1"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def width_transform(value: str) -> str:
    mappings = {"\uff03": "#"}
    mappings.update({chr(0xFF10 + i): str(i) for i in range(10)})
    return "".join(mappings.get(char, char) for char in value)


def classify(value: str, candidate: str) -> tuple[str, list[str]] | None:
    # The two clusters are intentionally mutually exclusive. A combined
    # transformation was not confirmed by the sample and is excluded.
    mapped = width_transform(value)
    if mapped != value and mapped == candidate:
        return mapped, ["format.confirmed_fullwidth_digit_number_sign_to_ascii"]
    if value.endswith((":", "\u00b7")) and value[:-1] == candidate:
        return candidate, ["format.confirmed_terminal_punctuation_trim"]
    return None


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    connection = connect_readonly(DB)
    rows = connection.execute(
        """
        SELECT c.candidate_id,c.batch_id,c.original_description,c.candidate_description,
          c.review_state,c.publication_state,c.validator_status,c.confidence,c.validator_version,
          d.source_snapshot_id,d.source_schema,d.source_asset_id,d.site_id,d.asset_number,
          d.source_row_hash,d.context_hash,d.location_code,d.location_description,d.location_parent,
          d.class_structure_description,d.classification_description
        FROM semantic_candidate c JOIN device_identity d ON d.device_id=c.device_id
        WHERE c.publication_state='unpublished' AND c.review_state='pending'
          AND c.validator_status='candidate' AND c.confidence='high'
          AND trim(d.original_description)<>''
        ORDER BY d.site_id,d.asset_number,c.candidate_id
        """
    ).fetchall()
    connection.close()

    columns = [
        "SITEID", "ASSETNUM", "ASSETID", "CANDIDATE_ID", "BATCH_ID", "SOURCE_SNAPSHOT_ID",
        "SOURCE_SCHEMA", "SOURCE_ROW_HASH", "CONTEXT_HASH", "ORIGINAL_DESCRIPTION",
        "PROPOSED_DESCRIPTION", "APPLIED_RULE_KEYS", "CLUSTER_STATUS", "LOCATION_CODE",
        "LOCATION_DESCRIPTION", "LOCATION_PARENT", "CLASSSTRUCTURE_DESCRIPTION",
        "CLASSIFICATION_DESCRIPTION", "REVIEW_STATE", "VALIDATOR_STATUS", "CONFIDENCE",
        "PREVIEW_STATUS",
    ]
    preview_rows: list[dict[str, str]] = []
    by_site: Counter[str] = Counter()
    by_rule: Counter[str] = Counter()
    examples: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        original = row["original_description"] or ""
        candidate = row["candidate_description"] or ""
        classified = classify(original, candidate)
        if classified is None:
            continue
        proposed, rule_keys = classified
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
            "PROPOSED_DESCRIPTION": proposed,
            "APPLIED_RULE_KEYS": "|".join(rule_keys),
            "CLUSTER_STATUS": "ai_sample_supported_strict_match",
            "LOCATION_CODE": row["location_code"] or "",
            "LOCATION_DESCRIPTION": row["location_description"] or "",
            "LOCATION_PARENT": row["location_parent"] or "",
            "CLASSSTRUCTURE_DESCRIPTION": row["class_structure_description"] or "",
            "CLASSIFICATION_DESCRIPTION": row["classification_description"] or "",
            "REVIEW_STATE": row["review_state"],
            "VALIDATOR_STATUS": row["validator_status"],
            "CONFIDENCE": row["confidence"],
            "PREVIEW_STATUS": "ready_for_cluster_confirmation",
        }
        preview_rows.append(item)
        by_site[item["SITEID"]] += 1
        for key in rule_keys:
            by_rule[key] += 1
            if len(examples[key]) < 10:
                examples[key].append({"site": item["SITEID"], "asset": item["ASSETNUM"], "original": original, "proposed": proposed})

    if len(preview_rows) != 251:
        raise SystemExit(f"Strict AI cluster preview expected 251 rows, found {len(preview_rows)}")
    if len({row["CANDIDATE_ID"] for row in preview_rows}) != 251:
        raise SystemExit("Preview contains duplicate candidate IDs")
    if any(row["REVIEW_STATE"] != "pending" or row["VALIDATOR_STATUS"] != "candidate" for row in preview_rows):
        raise SystemExit("Preview contains a non-pending or blocked candidate")

    with PREVIEW_CSV.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(preview_rows)
    with SAMPLE_CSV.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(preview_rows)

    summary = {
        "candidate_scope": len(rows),
        "preview_rows": len(preview_rows),
        "sample_rows": len(preview_rows),
        "by_site": dict(sorted(by_site.items())),
        "by_rule": dict(sorted(by_rule.items())),
        "cluster_examples": dict(examples),
        "source_write": False,
        "formal_publication": False,
        "rule_status": "proposed_not_active",
    }
    manifest = {
        "preview_id": f"ai-cluster-preview-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}",
        "generated_at_utc": utc_now(),
        "source_database": str(DB),
        "source_scope": "unpublished + pending + candidate + high confidence + strict match to AI-supported clusters",
        "preview_rows": len(preview_rows),
        "sample_rows": len(preview_rows),
        "rule_version": RULE_VERSION,
        "rule_keys": sorted(by_rule),
        "rule_status": "proposed_not_active",
        "preview_file": str(PREVIEW_CSV),
        "sample_file": str(SAMPLE_CSV),
        "preview_sha256": sha256(PREVIEW_CSV),
        "sample_sha256": sha256(SAMPLE_CSV),
        "source_write": False,
        "formal_publication": False,
        "approval_gate": "cluster confirmation, full replay, then batch approval",
        "summary": summary,
    }
    SUMMARY_JSON.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    MANIFEST_JSON.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"manifest": str(MANIFEST_JSON), "preview": str(PREVIEW_CSV), **summary}, ensure_ascii=False))


if __name__ == "__main__":
    main()
