"""Verify context-enriched HD candidate rows and their evidence hashes."""
from __future__ import annotations

import csv
import hashlib
import json
import pathlib
from collections import Counter

ROOT = pathlib.Path(__file__).resolve().parent
INPUT = ROOT / "context_candidates" / "equipment_description_context_candidates.csv"
REVIEW = ROOT / "context_candidates" / "context_review_queue.csv"
OUTPUT = ROOT / "context_candidates" / "verification.json"


def hash_context(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def main() -> None:
    statuses: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    ids: set[str] = set()
    rows = 0
    failures: list[str] = []
    with INPUT.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            rows += 1
            candidate_id = row.get("CANDIDATE_ID", "")
            if candidate_id in ids:
                failures.append("DUPLICATE_CANDIDATE_ID:" + candidate_id)
            ids.add(candidate_id)
            statuses[row.get("CONTEXT_STATUS", "")] += 1
            for reason in filter(None, row.get("CONTEXT_REASON_CODES", "").split(",")):
                reasons[reason] += 1
            try:
                context = json.loads(row.get("CONTEXT_JSON", "{}"))
            except json.JSONDecodeError:
                failures.append("INVALID_CONTEXT_JSON")
                continue
            if hash_context(context) != row.get("CONTEXT_HASH"):
                failures.append("CONTEXT_HASH_MISMATCH")
            if row.get("CONTEXT_STATUS") == "needs_review" and not row.get("CONTEXT_REASON_CODES"):
                failures.append("REVIEW_WITHOUT_REASON")
            if row.get("CONTEXT_STATUS") == "complete" and row.get("CONTEXT_REASON_CODES"):
                failures.append("COMPLETE_WITH_REASON")
    with REVIEW.open(encoding="utf-8-sig", newline="") as handle:
        review_rows = sum(1 for _ in csv.DictReader(handle))
    if review_rows != statuses.get("needs_review", 0):
        failures.append("REVIEW_QUEUE_COUNT_MISMATCH")
    result = {
        "status": "PASS" if not failures else "FAIL",
        "rows": rows,
        "distinct_candidate_ids": len(ids),
        "context_status_counts": dict(statuses),
        "context_reason_counts": dict(reasons),
        "review_queue_rows": review_rows,
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
