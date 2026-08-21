"""Replay the blocked-inspection evidence gates without applying any link."""
from __future__ import annotations

import argparse
import json
import pathlib
from collections import Counter
from datetime import datetime, timezone

from safe_convert import to_int


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--result-root", required=True)
    parser.add_argument("--analysis", required=True)
    parser.add_argument("--output", default="")
    args = parser.parse_args()
    result_root = pathlib.Path(args.result_root).resolve()
    analysis_path = pathlib.Path(args.analysis).resolve()
    payload = json.loads(analysis_path.read_text(encoding="utf-8"))
    replay_id = f"inspection-blocked-replay-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    failures = []
    validated = []
    for row in payload.get("rows", []):
        evidence = row.get("evidence") or {}
        action = row.get("recommended_action", "blocked_no_device_bridge")
        reasons = []
        if action != "candidate_direct_asset_number_bridge":
            reasons.append("no_direct_asset_number_bridge")
        if to_int(evidence.get("direct_asset_count", 0)) != 1:
            reasons.append("direct_asset_not_unique")
        if row.get("source_table") == "XJJL" and not row.get("description"):
            reasons.append("inspection_description_missing")
        if action == "needs_review_location_scope_not_device":
            reasons.append("location_scope_not_device_identity")
        if action in {"blocked_missing_location", "blocked_kks_not_found_in_asset", "blocked_business_record_without_device_bridge", "blocked_no_device_bridge"}:
            reasons.append(action)
        result = {
            "event_record_id": row.get("event_record_id"),
            "source_schema": row.get("source_schema"),
            "source_table": row.get("source_table"),
            "source_row_id": row.get("source_row_id"),
            "recommended_action": action,
            "reasons": sorted(set(reasons)),
        }
        if reasons:
            failures.append(result)
        else:
            validated.append(result)
    report = {
        "replay_id": replay_id,
        "analysis_file": str(analysis_path),
        "candidate_count": len(payload.get("rows", [])),
        "validated_count": len(validated),
        "failure_count": len(failures),
        "applied_count": 0,
        "failure_reason_counts": dict(Counter(reason for row in failures for reason in row["reasons"])),
        "action_counts": dict(Counter(row.get("recommended_action", "") for row in payload.get("rows", []))),
        "failures": failures,
        "validated": validated,
        "applied": False,
        "source_write": False,
        "formal_publication": False,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    output = pathlib.Path(args.output).resolve() if args.output else result_root / f"{replay_id}.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({**{key: report[key] for key in ("replay_id", "candidate_count", "validated_count", "failure_count", "applied_count", "failure_reason_counts")}, "output": str(output)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
