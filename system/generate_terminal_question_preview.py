"""Generate a read-only preview for removing only terminal question marks."""
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
OUTPUT_DIR = PROJECT_ROOT / "pilots" / "HD_SAAS" / "terminal_question_preview"
PREVIEW_CSV = OUTPUT_DIR / "rewrite_preview.csv"
MANIFEST_JSON = OUTPUT_DIR / "manifest.json"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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
          AND (instr(c.candidate_description, '?') > 0 OR instr(c.candidate_description, '？') > 0)
        ORDER BY d.site_id,d.asset_number,c.candidate_id
        """
    ).fetchall()
    connection.close()
    preview: list[dict[str, str]] = []
    for row in rows:
        original = row["original_description"] or ""
        candidate = row["candidate_description"] or ""
        if not candidate.endswith(("?", "？")):
            continue
        proposed = candidate[:-1].rstrip()
        if not proposed or proposed == candidate:
            continue
        preview.append({
            "SITEID": row["site_id"] or "", "ASSETNUM": row["asset_number"] or "", "ASSETID": row["source_asset_id"] or "",
            "CANDIDATE_ID": row["candidate_id"], "BATCH_ID": row["batch_id"], "SOURCE_SNAPSHOT_ID": row["source_snapshot_id"],
            "SOURCE_SCHEMA": row["source_schema"] or "", "SOURCE_ROW_HASH": row["source_row_hash"] or "", "CONTEXT_HASH": row["context_hash"] or "",
            "ORIGINAL_DESCRIPTION": original, "EXISTING_CANDIDATE_DESCRIPTION": candidate, "PROPOSED_DESCRIPTION": proposed,
            "DIFF_CATEGORY": "terminal_question_mark", "APPLIED_RULE_KEYS": "format.remove_terminal_question_mark",
            "LOCATION_CODE": row["location_code"] or "", "LOCATION_DESCRIPTION": row["location_description"] or "", "LOCATION_PARENT": row["location_parent"] or "",
            "CLASSSTRUCTURE_DESCRIPTION": row["class_structure_description"] or "", "CLASSIFICATION_DESCRIPTION": row["classification_description"] or "",
            "REVIEW_STATE": row["review_state"], "PUBLICATION_STATE": row["publication_state"], "VALIDATOR_STATUS": row["validator_status"], "CONFIDENCE": row["confidence"],
            "PREVIEW_STATUS": "proposed_not_active", "SOURCE_WRITE": "false", "FORMAL_PUBLICATION": "false",
        })
    if len(preview) != 42:
        raise SystemExit(f"Expected 42 terminal question rows, found {len(preview)}")
    with PREVIEW_CSV.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(preview[0].keys()))
        writer.writeheader(); writer.writerows(preview)
    manifest = {
        "preview_id": f"terminal-question-preview-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(), "preview_rows": len(preview),
        "rule_version": "terminal-question-proposed-20260812-v1", "rule_key": "format.remove_terminal_question_mark",
        "preview_file": str(PREVIEW_CSV), "preview_sha256": sha256(PREVIEW_CSV),
        "by_site": dict(sorted(Counter(row["SITEID"] for row in preview).items())),
        "source_write": False, "formal_publication": False, "status": "proposed_not_active",
        "guardrail": "only remove the final ASCII question mark from candidate description; internal question marks excluded",
    }
    MANIFEST_JSON.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False))


if __name__ == "__main__":
    main()
