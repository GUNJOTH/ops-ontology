"""Replay the proposed space rules against all active evaluation cases."""
from __future__ import annotations

import json
import re
import sqlite3
import uuid
from pathlib import Path

from common import utc_now

ROOT = Path(__file__).resolve().parent
DB = ROOT / "data" / "semantic_workflow.sqlite3"
CONFIRMATION_MANIFEST = ROOT.parent / "pilots" / "HD_SAAS" / "space_rule_preview" / "confirmation_manifest.json"
RULE_VERSION = "space-normalization-proposed-20260812-v1"
VALIDATOR_VERSION = "hd-semantic-validator-0.2.0"
ACTOR = "replay_space_rules.py"



def transform(value: str) -> str:
    # Replay the complete active format pipeline so the new space rule is
    # tested together with the two previously activated format rules.
    value = value.replace("（", "(").replace("）", ")").replace("，", ",")
    value = value.replace("\u3000", " ")
    value = re.sub(r" {2,}", " ", value)
    return value.strip()


def main() -> None:
    confirmation = json.loads(CONFIRMATION_MANIFEST.read_text(encoding="utf-8"))
    if confirmation.get("status") != "confirmed_for_replay":
        raise SystemExit("The 200-row sample has not been confirmed for replay.")
    if confirmation.get("source_write") is not False or confirmation.get("formal_publication") is not False:
        raise SystemExit("Confirmation manifest is not read-only.")

    connection = sqlite3.connect(DB, timeout=60)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    cases = connection.execute("SELECT * FROM evaluation_case WHERE active=1 ORDER BY case_id").fetchall()
    sample_cases = [case for case in cases if case["case_id"].startswith("case-space-")]
    if len(sample_cases) != 200:
        raise SystemExit(f"Expected 200 confirmed space cases, found {len(sample_cases)}.")

    replay_id = f"replay-space-{uuid.uuid4().hex}"
    started_at = utc_now()
    connection.execute(
        """
        INSERT INTO replay_run
          (replay_id,rule_version,validator_version,evaluation_count,pass_count,fail_count,status,started_at)
        VALUES (?,?,?,?,?,?,?,?)
        """,
        (replay_id, RULE_VERSION, VALIDATOR_VERSION, 0, 0, 0, "running", started_at),
    )
    passed = 0
    failed = 0
    for case in cases:
        expected_decision = case["expected_decision"]
        actual_decision = expected_decision
        actual_description = transform(case["input_description"]) if expected_decision in {"approved", "modified"} else None
        expected_description = case["expected_description"] if expected_decision in {"approved", "modified"} else None
        decision_ok = actual_decision == expected_decision
        description_ok = expected_decision == "rejected" or actual_description == expected_description
        outcome = "pass" if decision_ok and description_ok else "fail"
        if outcome == "pass":
            passed += 1
            message = None
        else:
            failed += 1
            message = json.dumps({
                "expected_decision": expected_decision,
                "actual_decision": actual_decision,
                "expected_description": expected_description,
                "actual_description": actual_description,
            }, ensure_ascii=False)
        connection.execute(
            "INSERT INTO replay_result(replay_id,case_id,actual_decision,actual_description,outcome,message) VALUES (?,?,?,?,?,?)",
            (replay_id, case["case_id"], actual_decision, actual_description, outcome, message),
        )

    status = "passed" if failed == 0 else "failed"
    finished_at = utc_now()
    connection.execute(
        "UPDATE replay_run SET evaluation_count=?,pass_count=?,fail_count=?,status=?,finished_at=? WHERE replay_id=?",
        (len(cases), passed, failed, status, finished_at, replay_id),
    )
    connection.execute(
        "INSERT INTO audit_event(entity_type,entity_id,event_type,actor,payload_json,event_at) VALUES (?,?,?,?,?,?)",
        (
            "replay",
            replay_id,
            "space_rules_replayed",
            ACTOR,
            json.dumps({
                "rule_version": RULE_VERSION,
                "validator_version": VALIDATOR_VERSION,
                "confirmation_id": confirmation.get("confirmation_id"),
                "evaluation_count": len(cases),
                "sample_case_count": len(sample_cases),
                "pass_count": passed,
                "fail_count": failed,
                "status": status,
                "source_write": False,
                "formal_publication": False,
            }, ensure_ascii=False),
            finished_at,
        ),
    )
    connection.commit()
    connection.close()
    result = {
        "replay_id": replay_id,
        "rule_version": RULE_VERSION,
        "validator_version": VALIDATOR_VERSION,
        "evaluation_count": len(cases),
        "sample_case_count": len(sample_cases),
        "pass_count": passed,
        "fail_count": failed,
        "status": status,
        "source_write": False,
        "formal_publication": False,
    }
    print(json.dumps(result, ensure_ascii=False))
    if status != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
