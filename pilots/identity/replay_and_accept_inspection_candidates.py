"""Replay and accept high-confidence inspection-to-device candidates locally."""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import sqlite3
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parent
INACTIVE_STATUSES = {"停用", "报废", "注销", "作废", "非活动", "废止", "退役"}


def latest_result() -> pathlib.Path:
    return sorted((ROOT / "results").glob("identity-layer-v1-*/"), reverse=True)[0]


def load_candidates(result_root: pathlib.Path) -> list[dict[str, object]]:
    path = result_root / "inspection_identity_candidates.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    return list(payload.get("candidates", []))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--result-root", default="")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--confirm-approval",
        action="store_true",
        help="明确确认本批次人工审批后，才允许把回放通过记录写为 accepted",
    )
    args = parser.parse_args()
    result_root = pathlib.Path(args.result_root).resolve() if args.result_root else latest_result()
    db_path = result_root / "identity_semantics.sqlite3"
    if args.apply and not args.confirm_approval:
        parser.error("--apply 仅能与 --confirm-approval 一起使用；回放通过不等于人工审批")
    candidates = load_candidates(result_root)
    candidate_hash = hashlib.sha256(
        (result_root / "inspection_identity_candidates.json").read_bytes()
    ).hexdigest()[:32]
    replay_id = f"inspection-replay-{candidate_hash}"
    connection = sqlite3.connect(str(db_path))
    failures: list[dict[str, object]] = []
    validated: list[dict[str, object]] = []
    for item in candidates:
        device = connection.execute(
            "SELECT status, location_code, canonical_name FROM unified_device WHERE unified_device_id=?",
            (item.get("candidate_device_id"),),
        ).fetchone()
        reasons: list[str] = []
        if item.get("recommended_action") != "candidate_for_auto_accept_after_replay":
            reasons.append("rule_gate_not_met")
        if item.get("location_device_count") != 1:
            reasons.append("location_not_unique")
        if (item.get("location_hierarchy_count") or 0) < 1:
            reasons.append("location_hierarchy_missing")
        if (item.get("description_similarity") or 0) < 0.9:
            reasons.append("description_similarity_below_threshold")
        if not device:
            reasons.append("candidate_device_missing")
        else:
            if device[1] != item.get("location_code"):
                reasons.append("device_location_mismatch")
            if device[0] in INACTIVE_STATUSES:
                reasons.append("inactive_device_status")
        result = {"event_record_id": item.get("event_record_id"), "candidate_device_id": item.get("candidate_device_id"), "reasons": reasons}
        if reasons:
            failures.append(result)
        else:
            validated.append({**result, "site_id": item.get("site_id"), "source_schema": item.get("source_schema"), "source_table": item.get("source_table"), "source_row_id": item.get("source_row_id"), "asset_number": item.get("candidate_asset_number"), "location_code": item.get("location_code"), "description_similarity": item.get("description_similarity"), "location_hierarchy_count": item.get("location_hierarchy_count")})

    applied = 0
    if args.apply and not failures:
        connection.execute(
            """CREATE TABLE IF NOT EXISTS inspection_identity_review (
                 review_id TEXT PRIMARY KEY,
                 replay_id TEXT NOT NULL,
                 event_record_id TEXT NOT NULL,
                 candidate_device_id TEXT NOT NULL,
                 decision TEXT NOT NULL,
                 rule_name TEXT NOT NULL,
                 evidence_json TEXT NOT NULL,
                 created_at TEXT NOT NULL
               )"""
        )
        reviewed_at = datetime.now(timezone.utc).isoformat()
        for item in validated:
            evidence = {
                "rule_name": "exact_location_unique_hierarchy_description",
                "location_device_count": 1,
                "location_hierarchy_count": item["location_hierarchy_count"],
                "description_similarity": item["description_similarity"],
                "kks_required": False,
                "source_write": False,
            }
            evidence_json = json.dumps(evidence, ensure_ascii=False, sort_keys=True)
            connection.execute(
                """INSERT INTO inspection_identity_review
                   VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(review_id) DO NOTHING""",
                (f"{replay_id}:{item['event_record_id']}", replay_id, item["event_record_id"], item["candidate_device_id"], "accepted", "exact_location_unique_hierarchy_description", evidence_json, reviewed_at),
            )
            connection.execute(
                """UPDATE device_event
                      SET unified_device_id=?, link_status='accepted', evidence_json=?
                    WHERE event_record_id=?""",
                (item["candidate_device_id"], evidence_json, item["event_record_id"]),
            )
            connection.execute(
                """UPDATE device_match_candidate
                      SET decision='accepted', match_method='exact_location_unique_hierarchy_description',
                          score=0.95, evidence_json=?
                    WHERE source_schema=? AND source_table=? AND source_row_id=?""",
                (evidence_json, item["source_schema"], item["source_table"], item["source_row_id"]),
            )
            connection.execute(
                """UPDATE device_identity_map
                      SET unified_device_id=?, status='accepted',
                          match_method='exact_location_unique_hierarchy_description', match_confidence=0.95,
                          evidence_json=?
                    WHERE source_schema=? AND source_table=? AND source_row_id=?""",
                (item["candidate_device_id"], evidence_json, item["source_schema"], item["source_table"], item["source_row_id"]),
            )
            applied += 1
        connection.commit()

    latest_count = connection.execute("SELECT COUNT(*) FROM v_latest_inspection").fetchone()[0]
    candidate_count = connection.execute("SELECT COUNT(*) FROM v_latest_inspection_candidate").fetchone()[0]
    connection.close()
    report = {
        "replay_id": replay_id,
        "candidate_count": len(candidates),
        "validated_count": len(validated),
        "failure_count": len(failures),
        "applied_count": applied,
        "latest_inspection_count": latest_count,
        "latest_inspection_candidate_count": candidate_count,
        "failures": failures,
        "validated": validated,
        "applied": bool(args.apply and args.confirm_approval and not failures),
        "approval_gate": "explicit_confirm_approval_required",
        "source_write": False,
        "formal_publication": False,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    (result_root / f"{replay_id}.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("replay_id", "candidate_count", "validated_count", "failure_count", "applied_count", "latest_inspection_count", "latest_inspection_candidate_count", "applied", "source_write")}, ensure_ascii=False))
    if failures:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
