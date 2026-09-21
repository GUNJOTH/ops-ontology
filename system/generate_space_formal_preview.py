"""Generate the formal read-only preview for active space rules."""
from __future__ import annotations

import csv
import hashlib
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from pipeline.contracts import connect_readonly

ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parent
DB = ROOT / "data" / "semantic_workflow.sqlite3"
OUTPUT_DIR = PROJECT_ROOT / "pilots" / "HD_SAAS" / "space_rule_preview"
PREVIEW_CSV = OUTPUT_DIR / "formal_space_rewrite_preview.csv"
MANIFEST_JSON = OUTPUT_DIR / "formal_preview_manifest.json"
SUMMARY_JSON = OUTPUT_DIR / "formal_preview_summary.json"
RULE_VERSION = "space-normalization-proposed-20260812-v1"
RULE_KEYS = (
    "format.fullwidth_space_to_ascii",
    "format.collapse_repeated_ascii_space",
    "format.trim_description_space",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def transform(value: str) -> tuple[str, list[str]]:
    result = value
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


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    connection = connect_readonly(DB)
    active = connection.execute(
        "SELECT rule_key,status,version FROM terminology_rule WHERE rule_key IN (?,?,?) ORDER BY rule_key",
        RULE_KEYS,
    ).fetchall()
    if len(active) != len(RULE_KEYS) or any(row["status"] != "active" or row["version"] != RULE_VERSION for row in active):
        connection.close()
        raise SystemExit("All three space rules must be active at the requested version.")
    rows = connection.execute(
        """
        SELECT c.candidate_id,c.batch_id,c.review_state,c.validator_status,c.confidence,
          c.original_description,c.rule_version,c.validator_version,
          d.source_snapshot_id,d.source_schema,d.source_asset_id,d.site_id,d.asset_number,
          d.source_row_hash,d.context_hash,d.location_code,d.location_description,d.location_parent,
          d.class_structure_description,d.classification_description
        FROM semantic_candidate c JOIN device_identity d ON d.device_id=c.device_id
        WHERE c.publication_state='unpublished' AND c.review_state='pending'
          AND (c.original_description LIKE ? OR c.original_description LIKE ?)
        ORDER BY d.site_id,d.asset_number,c.candidate_id
        """,
        ("%  %", "%\u3000%"),
    ).fetchall()
    connection.close()
    if len(rows) != 776:
        raise SystemExit(f"Formal preview scope changed: found {len(rows)}, expected 776.")

    columns = [
        "SITEID", "ASSETNUM", "ASSETID", "CANDIDATE_ID", "BATCH_ID", "SOURCE_SNAPSHOT_ID",
        "SOURCE_SCHEMA", "SOURCE_ROW_HASH", "CONTEXT_HASH", "ORIGINAL_DESCRIPTION",
        "PROPOSED_DESCRIPTION", "APPLIED_RULE_KEYS", "LOCATION_CODE", "LOCATION_DESCRIPTION",
        "LOCATION_PARENT", "CLASSSTRUCTURE_DESCRIPTION", "CLASSIFICATION_DESCRIPTION",
        "REVIEW_STATE", "VALIDATOR_STATUS", "CONFIDENCE", "PREVIEW_STATUS",
    ]
    preview_rows: list[dict[str, str]] = []
    by_site: Counter[str] = Counter()
    by_rule: Counter[str] = Counter()
    invalid: list[str] = []
    for row in rows:
        original = row["original_description"] or ""
        proposed, applied = transform(original)
        if not applied or proposed == original:
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
            "APPLIED_RULE_KEYS": "|".join(applied),
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
        for key in applied:
            by_rule[key] += 1
    if invalid or len({row["CANDIDATE_ID"] for row in preview_rows}) != len(preview_rows):
        raise SystemExit(json.dumps({"status": "BLOCKED", "invalid_count": len(invalid), "invalid": invalid[:20]}, ensure_ascii=False))

    with PREVIEW_CSV.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(preview_rows)
    generated_at = utc_now()
    summary = {
        "preview_rows": len(preview_rows),
        "by_site": dict(sorted(by_site.items())),
        "by_rule": dict(sorted(by_rule.items())),
        "sample_confirmation_id": json.loads((OUTPUT_DIR / "confirmation_manifest.json").read_text(encoding="utf-8"))["confirmation_id"],
    }
    manifest = {
        "preview_id": f"space-formal-preview-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}",
        "generated_at_utc": generated_at,
        "batch_id": preview_rows[0]["BATCH_ID"],
        "source_snapshot_id": preview_rows[0]["SOURCE_SNAPSHOT_ID"],
        "source_scope": "semantic_candidate where publication_state=unpublished and review_state=pending",
        "source_write": False,
        "formal_publication": False,
        "rule_version": RULE_VERSION,
        "rule_keys": list(RULE_KEYS),
        "rule_status": "active",
        "preview_file": str(PREVIEW_CSV),
        "preview_rows": len(preview_rows),
        "preview_sha256": sha256(PREVIEW_CSV),
        "summary": summary,
    }
    MANIFEST_JSON.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    SUMMARY_JSON.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"manifest": str(MANIFEST_JSON), "preview": str(PREVIEW_CSV), **summary}, ensure_ascii=False))


if __name__ == "__main__":
    main()
