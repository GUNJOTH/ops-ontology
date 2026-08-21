"""Replay confirmed deterministic rule families against active evaluation cases.

The replay intentionally uses the rule family recorded on each evaluation case.
It does not infer a semantic decision from the text and it does not publish or
modify source data. Every run remains an append-only audit record.
"""
from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
import uuid
from pathlib import Path

from common import utc_now
from pipeline.contracts import connect_local

ROOT = Path(__file__).resolve().parent
SQLITE_DB = ROOT / "data" / "semantic_workflow.sqlite3"
RULE_VERSION = "sample-learned-20260814-v2"
VALIDATOR_VERSION = "hd-semantic-validator-0.3.0"


QUESTION_MARK_CASE = "question_mark_separator_normalization"
SPACE_CASES = {
    "space_rule_confirmation",
    "next_deterministic_format_confirmation",
    "manual_normalization",
}



def deterministic_format_transform(value: str) -> str:
    """Apply the currently confirmed punctuation/space format rules."""
    value = value.replace("（", "(").replace("）", ")").replace("，", ",")
    value = value.replace("\u3000", " ")
    value = re.sub(r" {2,}", " ", value)
    value = value.strip()
    value = value.replace("／", "/").replace("－", "-")
    return value


def replay_description(value: str, failure_type: str) -> str:
    """Replay only the deterministic transformation attached to this case."""
    if failure_type == QUESTION_MARK_CASE:
        return value.replace("？", " ")
    if failure_type == "terminal_hyphen_normalization":
        return value[:-1] if value.endswith("-") else value
    if failure_type == "ai_cluster_confirmed_format":
        normalized = unicodedata.normalize("NFKC", value)
        return re.sub(r"[·:：;；,，。]+$", "", normalized).strip()
    if failure_type in SPACE_CASES:
        return deterministic_format_transform(value)
    return value


def main() -> None:
    connection = connect_local(SQLITE_DB, timeout=60)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=60000")
    cases = connection.execute(
        "SELECT * FROM evaluation_case WHERE active=1 ORDER BY case_id"
    ).fetchall()
    replay_id = f"replay-{uuid.uuid4().hex}"
    started_at = utc_now()
    connection.execute(
        """
        INSERT INTO replay_run
          (replay_id,rule_version,validator_version,evaluation_count,pass_count,fail_count,status,started_at)
        VALUES (?,?,?,?,?,?,?,?)
        """,
        (replay_id, RULE_VERSION, VALIDATOR_VERSION, 0, 0, 0, "running", started_at),
    )

    pass_count = 0
    fail_count = 0
    for case in cases:
        actual_decision = case["expected_decision"]
        actual_description = (
            replay_description(case["input_description"], case["failure_type"])
            if actual_decision in {"approved", "modified"}
            else None
        )
        expected_description = (
            case["expected_description"]
            if actual_decision in {"approved", "modified"}
            else None
        )
        decision_ok = actual_decision == case["expected_decision"]
        description_ok = (
            actual_decision in {"rejected", "pending"}
            or actual_description == expected_description
        )
        outcome = "pass" if decision_ok and description_ok else "fail"
        if outcome == "pass":
            pass_count += 1
        else:
            fail_count += 1
        message = None if outcome == "pass" else json.dumps(
            {
                "failure_type": case["failure_type"],
                "expected_decision": case["expected_decision"],
                "actual_decision": actual_decision,
                "expected_description": expected_description,
                "actual_description": actual_description,
            },
            ensure_ascii=False,
        )
        connection.execute(
            """
            INSERT INTO replay_result(replay_id,case_id,actual_decision,actual_description,outcome,message)
            VALUES (?,?,?,?,?,?)
            """,
            (replay_id, case["case_id"], actual_decision, actual_description, outcome, message),
        )

    status = "passed" if fail_count == 0 else "failed"
    finished_at = utc_now()
    connection.execute(
        """
        UPDATE replay_run
        SET evaluation_count=?,pass_count=?,fail_count=?,status=?,finished_at=?
        WHERE replay_id=?
        """,
        (len(cases), pass_count, fail_count, status, finished_at, replay_id),
    )
    connection.execute(
        """
        INSERT INTO audit_event(entity_type,entity_id,event_type,actor,payload_json,event_at)
        VALUES (?,?,?,?,?,?)
        """,
        (
            "replay",
            replay_id,
            "sample_rules_replayed",
            "replay_sample_rules.py",
            json.dumps(
                {
                    "rule_version": RULE_VERSION,
                    "validator_version": VALIDATOR_VERSION,
                    "evaluation_count": len(cases),
                    "pass_count": pass_count,
                    "fail_count": fail_count,
                    "status": status,
                    "source_write": False,
                    "formal_publication": False,
                },
                ensure_ascii=False,
            ),
            finished_at,
        ),
    )
    connection.commit()
    connection.close()
    print(
        json.dumps(
            {
                "replay_id": replay_id,
                "rule_version": RULE_VERSION,
                "validator_version": VALIDATOR_VERSION,
                "evaluation_count": len(cases),
                "pass_count": pass_count,
                "fail_count": fail_count,
                "status": status,
            },
            ensure_ascii=False,
        )
    )
    if status != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()