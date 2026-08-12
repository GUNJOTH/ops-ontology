"""Verify the approval-ready quality-only processing output."""
from __future__ import annotations

import csv
import hashlib
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent
INPUT = ROOT / "processed_quality" / "hd_processed_quality_candidates.csv"
MANIFEST = ROOT / "processed_quality" / "manifest.json"
OUTPUT = ROOT / "processed_quality" / "verification.json"


def hash_context(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def main() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    identities: set[tuple[str, str]] = set()
    candidate_ids: set[str] = set()
    rows = 0
    failures: list[str] = []
    with INPUT.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            rows += 1
            identity = (row.get("SITEID", ""), row.get("ASSETNUM", ""))
            candidate_id = row.get("CANDIDATE_ID", "")
            if identity in identities:
                failures.append("DUPLICATE_DEVICE_IDENTITY")
            identities.add(identity)
            if candidate_id in candidate_ids:
                failures.append("DUPLICATE_CANDIDATE_ID")
            candidate_ids.add(candidate_id)
            if row.get("CONTEXT_STATUS") != "complete" or row.get("CONTEXT_REASON_CODES"):
                failures.append("CONTEXT_WARNING_IN_PROCESSED_SET")
            if not row.get("UNIFIED_DESCRIPTION", "").strip():
                failures.append("EMPTY_UNIFIED_DESCRIPTION")
            if row.get("UNIFIED_DESCRIPTION") != row.get("CANDIDATE_DESCRIPTION"):
                failures.append("DESCRIPTION_CHANGED_WITHOUT_APPROVAL")
            if row.get("PROCESS_STATUS") != "ready_for_approval" or row.get("APPROVAL_STATUS") != "pending" or row.get("PUBLISH_STATUS") != "not_published":
                failures.append("INVALID_PROCESS_GATE_STATUS")
            try:
                context = json.loads(row.get("CONTEXT_JSON", "{}"))
            except json.JSONDecodeError:
                failures.append("INVALID_CONTEXT_JSON")
                continue
            if hash_context(context) != row.get("CONTEXT_HASH"):
                failures.append("CONTEXT_HASH_MISMATCH")
    if rows != int(manifest["processed_rows"]):
        failures.append("PROCESSED_ROW_COUNT_MISMATCH")
    result = {
        "status": "PASS" if not failures else "FAIL",
        "source_snapshot_id": manifest["source_snapshot_id"],
        "processed_rows": rows,
        "distinct_device_identities": len(identities),
        "distinct_candidate_ids": len(candidate_ids),
        "approval_status": "pending",
        "formal_publication": False,
        "failures": failures[:100],
        "failure_count": len(failures),
    }
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(result)
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
