"""Carry forward only previously replay-validated inspection bridges.

The source of truth is the prior local replay artifact.  This script ports
those accepted decisions into a newly rebuilt local identity result layer so
the full frozen baseline does not lose already validated evidence.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sqlite3
from datetime import datetime, timezone

from safe_convert import to_float, to_int


def main() -> None:
    parser = argparse.ArgumentParser(description="Port replay-validated inspection bridges into a rebuilt result layer")
    parser.add_argument("--source-result-root", required=True)
    parser.add_argument("--target-result-root", required=True)
    args = parser.parse_args()

    source_root = pathlib.Path(args.source_result_root).resolve()
    target_root = pathlib.Path(args.target_result_root).resolve()
    payload = json.loads((source_root / "inspection_identity_candidates.json").read_text(encoding="utf-8"))
    accepted = [
        row for row in payload.get("candidates", [])
        if row.get("recommended_action") == "candidate_for_auto_accept_after_replay"
    ]
    replay_id = "ported-" + str(payload.get("replay_id") or "inspection-replay-previous")
    applied: list[dict[str, object]] = []
    skipped: list[dict[str, object]] = []
    connection = sqlite3.connect(target_root / "identity_semantics.sqlite3")
    connection.execute("PRAGMA busy_timeout=30000")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS inspection_identity_review (
            review_id TEXT PRIMARY KEY,
            replay_id TEXT NOT NULL,
            event_record_id TEXT NOT NULL,
            candidate_device_id TEXT NOT NULL,
            decision TEXT NOT NULL,
            rule_name TEXT NOT NULL,
            evidence_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    now = datetime.now(timezone.utc).isoformat()
    for row in accepted:
        # The source replay is not sufficient on its own. Re-check the
        # rebuilt target layer before carrying an acceptance decision forward.
        if row.get("location_device_count") != 1:
            skipped.append({"event_record_id": row.get("event_record_id"), "reason": "source_location_not_unique"})
            continue
        if to_int(row.get("location_hierarchy_count")) < 1:
            skipped.append({"event_record_id": row.get("event_record_id"), "reason": "source_location_hierarchy_missing"})
            continue
        if to_float(row.get("description_similarity")) < 0.9:
            skipped.append({"event_record_id": row.get("event_record_id"), "reason": "source_similarity_below_threshold"})
            continue
        match = connection.execute(
            """
            SELECT candidate_id, candidate_device_id, evidence_json, decision
              FROM device_match_candidate
             WHERE source_schema=? AND source_table=? AND source_row_id=?
             ORDER BY score DESC, candidate_id
             LIMIT 1
            """,
            (row.get("source_schema", ""), row.get("source_table", ""), row.get("source_row_id", "")),
        ).fetchone()
        if not match or not match[1]:
            skipped.append({"event_record_id": row.get("event_record_id"), "reason": "target_candidate_missing"})
            continue
        if match[3] in {"rejected", "blocked"}:
            skipped.append({"event_record_id": row.get("event_record_id"), "reason": "target_candidate_blocked"})
            continue
        device = connection.execute(
            """
            SELECT location_code, status
              FROM unified_device
             WHERE unified_device_id=? AND master_source_schema=? AND site_id=?
            """,
            (match[1], row.get("source_schema", ""), row.get("site_id", "")),
        ).fetchone()
        if not device or device[0] != row.get("location_code") or str(device[1] or "") in {"停用", "报废", "注销", "作废", "非活动", "废止", "退役"}:
            skipped.append({"event_record_id": row.get("event_record_id"), "reason": "target_device_gate_failed"})
            continue
        target_location_count = connection.execute(
            "SELECT count(*) FROM unified_device WHERE master_source_schema=? AND site_id=? AND location_code=?",
            (row.get("source_schema", ""), row.get("site_id", ""), row.get("location_code", "")),
        ).fetchone()[0]
        if int(target_location_count) != 1:
            skipped.append({"event_record_id": row.get("event_record_id"), "reason": "target_location_not_unique"})
            continue
        evidence = {
            "rule_name": "exact_location_unique_hierarchy_description",
            "source_replay_id": replay_id,
            "location_device_count": row.get("location_device_count"),
            "location_hierarchy_count": row.get("location_hierarchy_count"),
            "description_similarity": row.get("description_similarity"),
            "kks_required": False,
            "source_write": False,
            "formal_publication": False,
        }
        evidence_json = json.dumps(evidence, ensure_ascii=False, sort_keys=True)
        connection.execute(
            """
            UPDATE device_match_candidate
               SET decision='accepted', match_method='exact_location_unique_hierarchy_description',
                   score=0.95, evidence_json=?
             WHERE candidate_id=?
            """,
            (evidence_json, match[0]),
        )
        connection.execute(
            """
            UPDATE device_identity_map
               SET unified_device_id=?, status='accepted',
                   match_method='exact_location_unique_hierarchy_description',
                   match_confidence=0.95, evidence_json=?
             WHERE source_schema=? AND source_table=? AND source_row_id=?
            """,
            (match[1], evidence_json, row.get("source_schema", ""), row.get("source_table", ""), row.get("source_row_id", "")),
        )
        connection.execute(
            """
            INSERT INTO inspection_identity_review
            VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(review_id) DO NOTHING
            """,
            (
                f"{replay_id}:{row.get('event_record_id')}", replay_id, row.get("event_record_id", ""),
                match[1], "accepted", "exact_location_unique_hierarchy_description", evidence_json, now,
            ),
        )
        applied.append({
            "event_record_id": row.get("event_record_id"),
            "source_schema": row.get("source_schema"),
            "source_table": row.get("source_table"),
            "source_row_id": row.get("source_row_id"),
            "candidate_device_id": match[1],
        })
    connection.commit()
    report = {
        "status": "ported_replay_validated_inspection_bridges",
        "source_replay_id": replay_id,
        "source_candidate_count": len(accepted),
        "applied_count": len(applied),
        "skipped_count": len(skipped),
        "applied": applied,
        "skipped": skipped,
        "source_write": False,
        "formal_publication": False,
        "created_at": now,
    }
    connection.close()
    output = target_root / "inspection-replay-port-report.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"report": str(output), **{key: report[key] for key in ("source_candidate_count", "applied_count", "skipped_count", "source_write", "formal_publication")}}, ensure_ascii=False))
    if skipped:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
