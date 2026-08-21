"""Verify that the HD quality set contains no context warnings or duplicate identities."""
from __future__ import annotations

import csv
import hashlib
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent
QUALITY_DIR = ROOT / "quality"
INPUT = QUALITY_DIR / "hd_quality_candidates.csv"
MANIFEST = QUALITY_DIR / "manifest.json"
OUTPUT = QUALITY_DIR / "verification.json"


def to_int(value: object, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


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
            if identity in identities:
                failures.append("DUPLICATE_DEVICE_IDENTITY")
            identities.add(identity)
            if row.get("CANDIDATE_ID", "") in candidate_ids:
                failures.append("DUPLICATE_CANDIDATE_ID")
            candidate_ids.add(row.get("CANDIDATE_ID", ""))
            if row.get("CONTEXT_STATUS") != "complete" or row.get("CONTEXT_REASON_CODES"):
                failures.append("QUALITY_ROW_HAS_CONTEXT_WARNING")
            if not row.get("CANDIDATE_DESCRIPTION", "").strip():
                failures.append("EMPTY_CANDIDATE_DESCRIPTION")
            try:
                context = json.loads(row.get("CONTEXT_JSON", "{}"))
            except json.JSONDecodeError:
                failures.append("INVALID_CONTEXT_JSON")
                continue
            if hash_context(context) != row.get("CONTEXT_HASH"):
                failures.append("CONTEXT_HASH_MISMATCH")
    if rows != to_int(manifest["quality_rows"]):
        failures.append("QUALITY_ROW_COUNT_MISMATCH")
    result = {
        "status": "PASS" if not failures else "FAIL",
        "source_snapshot_id": manifest["source_snapshot_id"],
        "quality_rows": rows,
        "distinct_device_identities": len(identities),
        "distinct_candidate_ids": len(candidate_ids),
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
