"""Exclude explicitly requested non-operational or unusable descriptions from the active set."""
from __future__ import annotations

import csv
import json
import os
import pathlib
from collections import Counter
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parent
CANDIDATES = ROOT / "candidates"
REVIEW = ROOT / "review"
INPUT = CANDIDATES / "equipment_description_candidates.csv"
ACTIVE_TMP = CANDIDATES / "equipment_description_candidates.active.tmp.csv"
ACTIVE = CANDIDATES / "equipment_description_candidates.csv"
EXCLUDED = REVIEW / "excluded_20260811.csv"
EXCLUSION_MANIFEST = CANDIDATES / "exclusion_manifest.json"
EXCLUDED_REASONS = {"STOPPED_DEVICE", "EMPTY_DESCRIPTION", "INSUFFICIENT_DESCRIPTION"}


def main() -> None:
    if not INPUT.exists():
        raise SystemExit("缺少候选文件")
    REVIEW.mkdir(parents=True, exist_ok=True)
    excluded_counts: Counter[str] = Counter()
    original_rows = 0
    active_rows = 0
    excluded_rows = 0
    with INPUT.open(encoding="utf-8-sig", newline="") as source_handle:
        reader = csv.DictReader(source_handle)
        if not reader.fieldnames:
            raise SystemExit("候选文件缺少表头")
        with ACTIVE_TMP.open("w", encoding="utf-8-sig", newline="") as active_handle, EXCLUDED.open("w", encoding="utf-8-sig", newline="") as excluded_handle:
            active_writer = csv.DictWriter(active_handle, fieldnames=reader.fieldnames)
            excluded_writer = csv.DictWriter(excluded_handle, fieldnames=reader.fieldnames)
            active_writer.writeheader()
            excluded_writer.writeheader()
            for row in reader:
                original_rows += 1
                reasons = set(filter(None, row.get("REASON_CODES", "").split(",")))
                matched = reasons & EXCLUDED_REASONS
                if matched:
                    excluded_writer.writerow(row)
                    excluded_rows += 1
                    for reason in matched:
                        excluded_counts[reason] += 1
                else:
                    active_writer.writerow(row)
                    active_rows += 1
    os.replace(ACTIVE_TMP, ACTIVE)
    manifest = {
        "exclusion_run_id": "hd-exclusion-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
        "source_snapshot_id": json.loads((ROOT / "snapshot" / "manifest.json").read_text(encoding="utf-8"))["source_snapshot_id"],
        "input": str(INPUT),
        "excluded_output": str(EXCLUDED),
        "criteria": sorted(EXCLUDED_REASONS),
        "original_rows": original_rows,
        "excluded_rows": excluded_rows,
        "active_rows": active_rows,
        "excluded_reason_counts": dict(excluded_counts),
        "source_write": False,
        "formal_publication": False,
    }
    EXCLUSION_MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(manifest)


if __name__ == "__main__":
    main()
