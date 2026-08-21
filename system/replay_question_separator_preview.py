"""Replay the proposed question-mark separator rule without publication."""
from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
from collections import Counter
from pathlib import Path

from common import utc_now
from pipeline.contracts import connect_local

ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parent
DB = ROOT / "data" / "semantic_workflow.sqlite3"
PREVIEW_DIR = PROJECT_ROOT / "pilots" / "HD_SAAS" / "question_separator_preview"
PREVIEW_CSV = PREVIEW_DIR / "rewrite_preview.csv"
MANIFEST_JSON = PREVIEW_DIR / "manifest.json"
REPLAY_MANIFEST = PREVIEW_DIR / "replay_manifest.json"
SUMMARY_JSON = PREVIEW_DIR / "replay_summary.json"

RULE_VERSION = "question-separator-proposed-20260813-v1"
RULE_KEY = "semantic.separator.fullwidth_question_mark_to_space"
VALIDATOR_VERSION = "question-separator-replay-validator-v1"
FULLWIDTH_QUESTION = "\uff1f"



def stable_replay_id(candidate_ids: list[str]) -> str:
    scope = "|".join(candidate_ids) + "|" + RULE_VERSION
    return f"replay-question-separator-{hashlib.sha256(scope.encode('utf-8')).hexdigest()[:20]}"


def load_preview() -> list[dict[str, str]]:
    with PREVIEW_CSV.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def main() -> None:
    if not PREVIEW_CSV.exists() or not MANIFEST_JSON.exists():
        raise SystemExit("Question separator preview is missing; generate it first.")
    manifest = json.loads(MANIFEST_JSON.read_text(encoding="utf-8"))
    if manifest.get("rule_version") != RULE_VERSION or manifest.get("rule_key") != RULE_KEY:
        raise SystemExit("Preview rule version or key does not match replay rule.")

    preview_rows = load_preview()
    if not preview_rows:
        raise SystemExit("Question separator preview is empty.")
    candidate_ids = [row["CANDIDATE_ID"] for row in preview_rows]
    if len(set(candidate_ids)) != len(candidate_ids):
        raise SystemExit("Preview contains duplicate candidate IDs.")

    connection = connect_local(str(DB), timeout=60)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=60000")
    try:
        placeholders = ",".join("?" for _ in candidate_ids)
        db_rows = connection.execute(
            f"""
            SELECT c.candidate_id,c.batch_id,c.original_description,c.candidate_description,
              c.confidence,c.validator_status,c.review_state,c.publication_state,
              c.rule_version,c.validator_version,
              d.source_snapshot_id,d.source_schema,d.source_asset_id,d.site_id,d.asset_number,
              d.location_code,d.location_description,d.location_parent,
              d.classification_description,d.class_structure_description
            FROM semantic_candidate c
            JOIN device_identity d ON d.device_id=c.device_id
            WHERE c.candidate_id IN ({placeholders})
            """,
            candidate_ids,
        ).fetchall()
        db_by_id = {row["candidate_id"]: row for row in db_rows}
        failures: list[dict[str, str]] = []
        replay_rows: list[dict[str, object]] = []
        by_class: Counter[str] = Counter()
        by_site: Counter[str] = Counter()

        for preview in preview_rows:
            candidate_id = preview["CANDIDATE_ID"]
            row = db_by_id.get(candidate_id)
            checks: dict[str, bool] = {}
            if row is None:
                failures.append({"candidate_id": candidate_id, "check": "candidate_exists"})
                continue

            original = row["original_description"] or ""
            proposed = original.replace(FULLWIDTH_QUESTION, " ")
            positions = [index for index, char in enumerate(original) if char == FULLWIDTH_QUESTION]
            expected_class = "interior_multiple" if len(positions) > 1 else "interior_single"
            checks["preview_original_matches_db"] = preview["ORIGINAL_DESCRIPTION"] == original
            checks["preview_candidate_matches_db"] = preview["EXISTING_CANDIDATE_DESCRIPTION"] == (row["candidate_description"] or "")
            checks["pending_unpublished"] = row["review_state"] == "pending" and row["publication_state"] == "unpublished"
            checks["high_quality_candidate"] = row["confidence"] == "high" and row["validator_status"] == "candidate"
            checks["has_fullwidth_question"] = bool(positions)
            checks["all_questions_are_interior"] = bool(positions) and all(0 < index < len(original) - 1 for index in positions)
            checks["preview_class_matches"] = preview["QUESTION_MARK_CLASS"] == expected_class
            checks["preview_count_matches"] = preview["QUESTION_MARK_COUNT"] == str(len(positions))
            checks["proposed_matches_rule"] = preview["PROPOSED_DESCRIPTION"] == proposed
            checks["only_question_marks_changed"] = preview["PROPOSED_DESCRIPTION"] == original.replace(FULLWIDTH_QUESTION, " ")
            checks["no_question_mark_remains"] = FULLWIDTH_QUESTION not in preview["PROPOSED_DESCRIPTION"]

            for check, passed in checks.items():
                if not passed:
                    failures.append({"candidate_id": candidate_id, "check": check})
            replay_rows.append({
                "candidate_id": candidate_id,
                "case_id": f"case-question-separator-{candidate_id}",
                "actual_description": proposed,
                "passed": all(checks.values()),
            })
            by_class[expected_class] += 1
            by_site[row["site_id"] or "(空)"] += 1

        if len(db_rows) != len(preview_rows):
            failures.append({"candidate_id": "(scope)", "check": "preview_db_row_count_matches"})
        if not replay_rows:
            raise SystemExit("No replay rows were built.")

        replay_id = stable_replay_id(candidate_ids)
        now = utc_now()
        connection.execute("BEGIN IMMEDIATE")
        for preview, replay_row in zip(preview_rows, replay_rows):
            row = db_by_id.get(preview["CANDIDATE_ID"])
            if row is None:
                continue
            context = {
                "rule_key": RULE_KEY,
                "question_mark_class": preview["QUESTION_MARK_CLASS"],
                "question_mark_count": int(preview["QUESTION_MARK_COUNT"]),
                "candidate_description_before_rule": row["candidate_description"] or "",
                "location_code": row["location_code"] or "",
                "location_description": row["location_description"] or "",
                "location_parent": row["location_parent"] or "",
                "classification_description": row["classification_description"] or "",
                "source_write": False,
                "formal_approval": False,
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
                    replay_row["case_id"], None, row["source_snapshot_id"], row["source_schema"],
                    row["site_id"], row["asset_number"], row["original_description"],
                    json.dumps(context, ensure_ascii=False, sort_keys=True), "modified",
                    preview["PROPOSED_DESCRIPTION"], "question_mark_separator_normalization",
                    1, RULE_VERSION, now,
                ),
            )

        existing = connection.execute("SELECT * FROM replay_run WHERE replay_id=?", (replay_id,)).fetchone()
        if existing is None:
            passed = sum(1 for item in replay_rows if item["passed"]) and len(replay_rows) - len(failures) if False else sum(1 for item in replay_rows if item["passed"])
            failed = len(replay_rows) - passed
            if any(item.get("candidate_id") == "(scope)" for item in failures):
                failed += 1
            status = "passed" if not failures else "failed"
            connection.execute(
                """
                INSERT INTO replay_run
                  (replay_id,rule_version,validator_version,evaluation_count,pass_count,fail_count,status,started_at,finished_at)
                VALUES (?,?,?,?,?,?,?,?,?)
                """,
                (replay_id, RULE_VERSION, VALIDATOR_VERSION, len(replay_rows), passed, failed, status, now, utc_now()),
            )
            for item in replay_rows:
                connection.execute(
                    "INSERT INTO replay_result(replay_id,case_id,actual_decision,actual_description,outcome,message) VALUES (?,?,?,?,?,?)",
                    (replay_id, item["case_id"], "modified", item["actual_description"], "pass" if item["passed"] else "fail", None if item["passed"] else "question separator replay validation failed"),
                )
            connection.execute(
                """
                INSERT INTO audit_event(entity_type,entity_id,event_type,actor,payload_json,event_at)
                VALUES (?,?,?,?,?,?)
                """,
                (
                    "replay_run", replay_id, "question_separator_replay_completed", "question-separator-replay",
                    json.dumps({"candidate_count": len(replay_rows), "pass_count": passed, "fail_count": failed, "source_write": False, "formal_publication": False}, ensure_ascii=False), now,
                ),
            )
        else:
            passed = int(existing["pass_count"])
            failed = int(existing["fail_count"])
            status = existing["status"]

        connection.commit()
        payload = {
            "status": status,
            "replay_id": replay_id,
            "rule_key": RULE_KEY,
            "rule_version": RULE_VERSION,
            "validator_version": VALIDATOR_VERSION,
            "evaluation_count": len(replay_rows),
            "pass_count": passed,
            "fail_count": failed,
            "preview_rows": len(preview_rows),
            "by_question_mark_class": dict(sorted(by_class.items())),
            "by_site": dict(sorted(by_site.items())),
            "source_write": False,
            "formal_approval_created": False,
            "formal_publication": False,
            "failures": failures,
        }
        SUMMARY_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        REPLAY_MANIFEST.write_text(json.dumps({**payload, "generated_at_utc": utc_now(), "scope": "question separator preview only", "summary_file": str(SUMMARY_JSON)}, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(payload, ensure_ascii=False))
    finally:
        connection.close()


if __name__ == "__main__":
    main()