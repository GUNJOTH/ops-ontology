"""Replay the confirmed AI keep-original clusters only.

This is a local replay gate. It records evaluation cases and replay results,
but does not create a formal review approval or publish anything.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parent
DB = ROOT / "data" / "semantic_workflow.sqlite3"
OUTPUT_DIR = PROJECT_ROOT / "pilots" / "HD_SAAS" / "ai_cluster_keep_original_replay"
MANIFEST = OUTPUT_DIR / "replay_manifest.json"
SUMMARY = OUTPUT_DIR / "summary.json"
RULE_VERSION = "ai-cluster-keep-original-v1"
VALIDATOR_VERSION = "ai-cluster-keep-original-replay-validator-v1"

BACKEND_ROOT = PROJECT_ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))
from app.main import ai_cluster_id, classify_ai_cluster, cluster_pattern  # noqa: E402


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def stable_replay_id(candidate_ids: list[str]) -> str:
    scope_hash = hashlib.sha256("|".join(candidate_ids).encode("utf-8")).hexdigest()[:20]
    return f"replay-ai-cluster-keep-original-{scope_hash}"


def main() -> None:
    if not DB.exists():
        raise SystemExit(f"SQLite database not found: {DB}")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(DB), timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        source_rows = connection.execute(
            """
            SELECT c.candidate_id,c.batch_id,c.original_description,c.candidate_description,
              c.confidence,c.validator_status,c.review_state,c.publication_state,
              c.rule_version,c.validator_version,
              d.source_snapshot_id,d.source_schema,d.source_asset_id,d.site_id,d.asset_number,
              d.location_code,d.location_description,d.location_parent,d.classification_description,
              a.decision AS ai_decision,a.sample_id
            FROM semantic_candidate c
            JOIN device_identity d ON d.device_id=c.device_id
            LEFT JOIN ai_review_decision a ON a.candidate_id=c.candidate_id
            WHERE c.review_state='pending'
              AND c.publication_state='unpublished'
              AND c.validator_status='candidate'
              AND c.confidence='high'
              AND c.evidence_level='strong'
              AND length(trim(c.original_description)) > 0
              AND length(trim(c.candidate_description)) > 0
              AND c.original_description <> c.candidate_description
            ORDER BY d.site_id,d.asset_number,c.candidate_id
            """
        ).fetchall()
        confirmed_clusters = {
            row["cluster_id"]: row["decision"]
            for row in connection.execute("SELECT cluster_id,decision FROM ai_cluster_decision WHERE decision='keep_original'").fetchall()
        }
        rows: list[dict[str, object]] = []
        for source in source_rows:
            kind, _label, _recommendation, _confidence, _reason = classify_ai_cluster(source["original_description"] or "", source["candidate_description"] or "")
            pattern = cluster_pattern(kind, source["original_description"] or "", source["candidate_description"] or "")
            cluster_id = ai_cluster_id(kind, pattern)
            if cluster_id not in confirmed_clusters:
                continue
            item = dict(source)
            item["cluster_id"] = cluster_id
            item["cluster_decision"] = confirmed_clusters[cluster_id]
            rows.append(item)
        if len(rows) != 17:
            raise SystemExit(f"Expected exactly 17 confirmed keep-original members, found {len(rows)}")
        candidate_ids = [row["candidate_id"] for row in rows]
        if len(set(candidate_ids)) != len(candidate_ids):
            raise SystemExit("Duplicate candidate IDs in replay scope")

        failures: list[dict[str, str]] = []
        for row in rows:
            checks = {
                "member_decision_compatible": row["ai_decision"] in (None, "keep_original"),
                "cluster_decision": row["cluster_decision"] == "keep_original",
                "high_quality": row["confidence"] == "high" and row["validator_status"] == "candidate",
                "pending_unpublished": row["review_state"] == "pending" and row["publication_state"] == "unpublished",
                "non_empty": bool(str(row["original_description"] or "").strip()) and bool(str(row["candidate_description"] or "").strip()),
                "leading_minus_shape": str(row["original_description"] or "").startswith("-") and str(row["original_description"] or "")[1:] == str(row["candidate_description"] or ""),
            }
            for check, passed in checks.items():
                if not passed:
                    failures.append({"candidate_id": row["candidate_id"], "check": check})

        replay_id = stable_replay_id(candidate_ids)
        now = utc_now()
        connection.execute("BEGIN IMMEDIATE")
        for row in rows:
            case_id = f"case-ai-cluster-keep-original-{row['candidate_id']}"
            context = {
                "cluster_id": row["cluster_id"],
                "ai_sample_id": row["sample_id"],
                "candidate_description": row["candidate_description"],
                "location_code": row["location_code"] or "",
                "location_description": row["location_description"] or "",
                "location_parent": row["location_parent"] or "",
                "classification_description": row["classification_description"] or "",
                "source_write": False,
                "formal_publication": False,
            }
            connection.execute(
                """
                INSERT OR IGNORE INTO evaluation_case
                  (case_id,source_review_id,source_snapshot_id,source_schema,site_id,asset_number,
                   input_description,context_json,expected_decision,expected_description,failure_type,
                   active,introduced_rule_version,created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    case_id,
                    None,
                    row["source_snapshot_id"],
                    row["source_schema"],
                    row["site_id"],
                    row["asset_number"],
                    row["original_description"],
                    json.dumps(context, ensure_ascii=False, sort_keys=True),
                    "keep_original",
                    row["original_description"],
                    "ai_cluster_leading_minus_keep_original",
                    1,
                    RULE_VERSION,
                    now,
                ),
            )

        existing = connection.execute("SELECT * FROM replay_run WHERE replay_id=?", (replay_id,)).fetchone()
        if existing is None:
            replay_rows: list[tuple[str, str | None, str]] = []
            for row in rows:
                case_id = f"case-ai-cluster-keep-original-{row['candidate_id']}"
                passed = not failures or not any(item["candidate_id"] == row["candidate_id"] for item in failures)
                replay_rows.append(
                    (
                        case_id,
                        row["original_description"],
                        "pass" if passed else "fail",
                    )
                )
            pass_count = sum(1 for _, _, outcome in replay_rows if outcome == "pass")
            fail_count = len(replay_rows) - pass_count
            connection.execute(
                """
                INSERT INTO replay_run
                  (replay_id,rule_version,validator_version,evaluation_count,pass_count,fail_count,status,started_at,finished_at)
                VALUES (?,?,?,?,?,?,?,?,?)
                """,
                (replay_id, RULE_VERSION, VALIDATOR_VERSION, len(replay_rows), pass_count, fail_count, "passed" if fail_count == 0 else "failed", now, utc_now()),
            )
            for case_id, actual_description, outcome in replay_rows:
                connection.execute(
                    "INSERT INTO replay_result(replay_id,case_id,actual_decision,actual_description,outcome,message) VALUES (?,?,?,?,?,?)",
                    (replay_id, case_id, "keep_original", actual_description, outcome, None if outcome == "pass" else "keep-original replay validation failed"),
                )
            connection.execute(
                """
                INSERT INTO audit_event(entity_type,entity_id,event_type,actor,payload_json,event_at)
                VALUES (?,?,?,?,?,?)
                """,
                (
                    "replay_run",
                    replay_id,
                    "ai_cluster_keep_original_replay_completed",
                    "cluster-replay",
                    json.dumps({"candidate_count": len(rows), "pass_count": pass_count, "fail_count": fail_count, "source_write": False, "formal_publication": False}, ensure_ascii=False),
                    utc_now(),
                ),
            )
        else:
            pass_count = int(existing["pass_count"])
            fail_count = int(existing["fail_count"])

        connection.commit()
        status = "passed" if fail_count == 0 and not failures else "failed"
        payload = {
            "status": status,
            "replay_id": replay_id,
            "rule_version": RULE_VERSION,
            "validator_version": VALIDATOR_VERSION,
            "evaluation_count": len(rows),
            "pass_count": pass_count,
            "fail_count": fail_count,
            "candidate_ids": candidate_ids,
            "cluster_count": len({row["cluster_id"] for row in rows}),
            "site_counts": {site: sum(1 for row in rows if row["site_id"] == site) for site in sorted({row["site_id"] for row in rows})},
            "source_write": False,
            "formal_publication": False,
            "formal_approval_created": False,
            "failures": failures,
        }
        SUMMARY.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        MANIFEST.write_text(json.dumps({**payload, "generated_at_utc": utc_now(), "scope": "7 explicitly confirmed keep-original AI clusters only", "summary_file": str(SUMMARY)}, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(payload, ensure_ascii=False))
    finally:
        connection.close()


if __name__ == "__main__":
    main()
