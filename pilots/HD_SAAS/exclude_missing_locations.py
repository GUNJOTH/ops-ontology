"""Exclude devices whose ASSET.LOCATION has no matching LOCATIONS row."""
from __future__ import annotations

import csv
import json
import os
import pathlib
from collections import Counter
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parent
CANDIDATES = ROOT / "candidates"
CONTEXT = ROOT / "context_candidates"
REVIEW = ROOT / "review"
ACTIVE_CANDIDATES = CANDIDATES / "equipment_description_candidates.csv"
ACTIVE_CANDIDATES_TMP = CANDIDATES / "equipment_description_candidates.location.tmp.csv"
CONTEXT_INPUT = CONTEXT / "equipment_description_context_candidates.csv"
CONTEXT_OUTPUT_TMP = CONTEXT / "equipment_description_context_candidates.location.tmp.csv"
CONTEXT_REVIEW_TMP = CONTEXT / "context_review_queue.location.tmp.csv"
EXCLUDED_CONTEXT = REVIEW / "excluded_missing_location_20260811.csv"
LOCATION_MANIFEST = CANDIDATES / "location_exclusion_manifest.json"


def main() -> None:
    missing_ids: set[str] = set()
    excluded_count = 0
    with CONTEXT_INPUT.open(encoding="utf-8-sig", newline="") as source_handle, EXCLUDED_CONTEXT.open("w", encoding="utf-8-sig", newline="") as excluded_handle:
        reader = csv.DictReader(source_handle)
        if not reader.fieldnames:
            raise SystemExit("上下文候选缺少表头")
        excluded_writer = csv.DictWriter(excluded_handle, fieldnames=reader.fieldnames)
        excluded_writer.writeheader()
        for row in reader:
            reasons = set(filter(None, row.get("CONTEXT_REASON_CODES", "").split(",")))
            if "MISSING_LOCATION" in reasons:
                candidate_id = row.get("CANDIDATE_ID", "")
                missing_ids.add(candidate_id)
                excluded_writer.writerow(row)
                excluded_count += 1
    if excluded_count != len(missing_ids):
        raise SystemExit("缺失位置记录出现重复 candidate_id，未执行排除")

    candidate_excluded = 0
    with ACTIVE_CANDIDATES.open(encoding="utf-8-sig", newline="") as source_handle:
        reader = csv.DictReader(source_handle)
        if not reader.fieldnames:
            raise SystemExit("候选文件缺少表头")
        with ACTIVE_CANDIDATES_TMP.open("w", encoding="utf-8-sig", newline="") as active_handle:
            writer = csv.DictWriter(active_handle, fieldnames=reader.fieldnames)
            writer.writeheader()
            for row in reader:
                if row.get("CANDIDATE_ID", "") in missing_ids:
                    candidate_excluded += 1
                else:
                    writer.writerow(row)
    os.replace(ACTIVE_CANDIDATES_TMP, ACTIVE_CANDIDATES)
    if candidate_excluded != excluded_count:
        raise SystemExit(f"候选与上下文排除数量不一致: {candidate_excluded}!={excluded_count}")

    context_counts: Counter[str] = Counter()
    context_reasons: Counter[str] = Counter()
    with CONTEXT_INPUT.open(encoding="utf-8-sig", newline="") as source_handle:
        reader = csv.DictReader(source_handle)
        with CONTEXT_OUTPUT_TMP.open("w", encoding="utf-8-sig", newline="") as output_handle, CONTEXT_REVIEW_TMP.open("w", encoding="utf-8-sig", newline="") as review_handle:
            writer = csv.DictWriter(output_handle, fieldnames=reader.fieldnames)
            review_writer = csv.DictWriter(review_handle, fieldnames=reader.fieldnames)
            writer.writeheader()
            review_writer.writeheader()
            for row in reader:
                if row.get("CANDIDATE_ID", "") in missing_ids:
                    continue
                writer.writerow(row)
                status = row.get("CONTEXT_STATUS", "")
                context_counts[status] += 1
                for reason in filter(None, row.get("CONTEXT_REASON_CODES", "").split(",")):
                    context_reasons[reason] += 1
                if status == "needs_review":
                    review_writer.writerow(row)
    os.replace(CONTEXT_OUTPUT_TMP, CONTEXT_INPUT)
    os.replace(CONTEXT_REVIEW_TMP, CONTEXT / "context_review_queue.csv")

    snapshot = json.loads((ROOT / "snapshot" / "manifest.json").read_text(encoding="utf-8"))
    manifest = {
        "location_exclusion_run_id": "hd-location-exclusion-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
        "source_snapshot_id": snapshot["source_snapshot_id"],
        "criteria": "ASSET.LOCATION has no matching HD_SAAS.LOCATIONS row within (SITEID, LOCATION)",
        "excluded_rows": excluded_count,
        "previous_active_candidate_rows": 508933,
        "current_active_candidate_rows": 508933 - excluded_count,
        "excluded_output": str(EXCLUDED_CONTEXT),
        "context_status_counts_after_exclusion": dict(context_counts),
        "context_reason_counts_after_exclusion": dict(context_reasons),
        "source_write": False,
        "formal_publication": False,
    }
    LOCATION_MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(manifest)


if __name__ == "__main__":
    main()
