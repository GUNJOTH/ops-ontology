"""Verify the HD_SAAS snapshot-to-candidate batch without publishing anything."""
from __future__ import annotations

import csv
import json
import pathlib
from collections import Counter

ROOT = pathlib.Path(__file__).resolve().parent
SNAPSHOT_MANIFEST = ROOT / "snapshot" / "manifest.json"
CANDIDATE_MANIFEST = ROOT / "candidates" / "manifest.json"
CANDIDATE_FILE = ROOT / "candidates" / "equipment_description_candidates.csv"
EXCLUSION_MANIFEST = ROOT / "candidates" / "exclusion_manifest.json"
LOCATION_EXCLUSION_MANIFEST = ROOT / "candidates" / "location_exclusion_manifest.json"
OUTPUT = ROOT / "candidates" / "verification.json"


def to_int(value: object, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def main() -> None:
    snapshot = json.loads(SNAPSHOT_MANIFEST.read_text(encoding="utf-8"))
    candidate_manifest = json.loads(CANDIDATE_MANIFEST.read_text(encoding="utf-8"))
    exclusion = json.loads(EXCLUSION_MANIFEST.read_text(encoding="utf-8")) if EXCLUSION_MANIFEST.exists() else None
    location_exclusion = json.loads(LOCATION_EXCLUSION_MANIFEST.read_text(encoding="utf-8")) if LOCATION_EXCLUSION_MANIFEST.exists() else None
    counts: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    identities: set[tuple[str, str]] = set()
    candidate_ids: set[str] = set()
    failures: list[str] = []
    rows = 0
    with CANDIDATE_FILE.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            rows += 1
            identity = (row.get("SITEID", ""), row.get("ASSETNUM", ""))
            candidate_id = row.get("CANDIDATE_ID", "")
            if identity in identities:
                failures.append("DUPLICATE_DEVICE_IDENTITY:" + "|".join(identity))
            identities.add(identity)
            if candidate_id in candidate_ids:
                failures.append("DUPLICATE_CANDIDATE_ID:" + candidate_id)
            candidate_ids.add(candidate_id)
            counts[row.get("RESULT_STATUS", "")] += 1
            for reason in filter(None, row.get("REASON_CODES", "").split(",")):
                reasons[reason] += 1
            if row.get("SOURCE_SNAPSHOT_ID") != snapshot.get("source_snapshot_id"):
                failures.append("SOURCE_SNAPSHOT_MISMATCH")
            if not row.get("SOURCE_ROW_HASH"):
                failures.append("MISSING_SOURCE_ROW_HASH")
            if not row.get("RULE_VERSION") or not row.get("VALIDATOR_VERSION"):
                failures.append("MISSING_VERSION_TRACE")
            if row.get("RESULT_STATUS") == "candidate" and not row.get("CANDIDATE_DESCRIPTION", "").strip():
                failures.append("EMPTY_CANDIDATE_DESCRIPTION")
            try:
                json.loads(row.get("EVIDENCE_JSON", "{}"))
            except json.JSONDecodeError:
                failures.append("INVALID_EVIDENCE_JSON")

    excluded_rows = (to_int(exclusion["excluded_rows"]) if exclusion else 0) + (to_int(location_exclusion["excluded_rows"]) if location_exclusion else 0)
    expected_rows = to_int(snapshot["source_row_count"]) - excluded_rows
    if rows != expected_rows:
        failures.append(f"ROW_COUNT_MISMATCH:{rows}!={expected_rows}")
    if len(identities) != to_int(snapshot["distinct_identity_count"]) - excluded_rows:
        failures.append("IDENTITY_COUNT_MISMATCH")
    if counts.get("blocked", 0):
        failures.append("BLOCKED_RESULTS_PRESENT")

    result = {
        "status": "PASS" if not failures else "FAIL",
        "source_snapshot_id": snapshot["source_snapshot_id"],
        "source_rows": expected_rows,
        "candidate_rows": rows,
        "excluded_rows": excluded_rows,
        "distinct_device_identities": len(identities),
        "distinct_candidate_ids": len(candidate_ids),
        "result_status_counts": dict(counts),
        "reason_counts": dict(reasons),
        "failures": failures[:100],
        "failure_count": len(failures),
        "formal_publication": False,
    }
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(result)
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
