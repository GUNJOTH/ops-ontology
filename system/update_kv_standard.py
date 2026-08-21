"""Update the confirmed semantic baseline from KV to kV, without publication."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DB = ROOT / "data" / "semantic_workflow.sqlite3"
RULE_KEY = "format.unit.uppercase_kv_to_kv"
RULE_VERSION = "kv-standard-proposed-20260814-v1"
ACTOR = "semantic-standard-change"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def main() -> None:
    connection = sqlite3.connect(str(DB), timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=30000")
    now = utc_now()
    try:
        proposal = connection.execute(
            "SELECT * FROM rule_agent_proposal WHERE rule_key=? ORDER BY created_at DESC LIMIT 1",
            (RULE_KEY,),
        ).fetchone()
        if proposal is None or not proposal["evaluation_replay_id"]:
            raise SystemExit("No prior KV rule replay was found")

        failed_results = connection.execute(
            """
            SELECT case_id,actual_description
            FROM replay_result
            WHERE replay_id=? AND outcome='fail'
            ORDER BY case_id
            """,
            (proposal["evaluation_replay_id"],),
        ).fetchall()
        if len(failed_results) != 8:
            raise SystemExit(f"Expected exactly 8 KV baseline conflicts, found {len(failed_results)}")

        case_ids = [row["case_id"] for row in failed_results]
        actual_by_case = {row["case_id"]: row["actual_description"] for row in failed_results}
        cases = connection.execute(
            "SELECT * FROM evaluation_case WHERE case_id IN ({})".format(",".join("?" for _ in case_ids)),
            tuple(case_ids),
        ).fetchall()
        if len(cases) != 8 or any(not row["active"] for row in cases):
            raise SystemExit("The 8 KV cases are not all active")

        connection.execute("BEGIN IMMEDIATE")
        updated_cases: list[dict[str, str]] = []
        for case in cases:
            context = json.loads(case["context_json"] or "{}")
            if not isinstance(context, dict):
                context = {}
            context["semanticStandardUpdate"] = {
                "sourceTerm": "KV",
                "targetTerm": "kV",
                "ruleKey": RULE_KEY,
                "ruleVersion": RULE_VERSION,
                "updatedAt": now,
                "previousReplayId": proposal["evaluation_replay_id"],
            }
            new_description = actual_by_case[case["case_id"]]
            connection.execute(
                """
                UPDATE evaluation_case
                SET expected_decision='modified',expected_description=?,context_json=?,introduced_rule_version=?
                WHERE case_id=? AND active=1
                """,
                (new_description, json.dumps(context, ensure_ascii=False), RULE_VERSION, case["case_id"]),
            )
            updated_cases.append(
                {
                    "caseId": case["case_id"],
                    "before": case["expected_description"],
                    "after": new_description,
                    "decision": "modified",
                }
            )

        term = connection.execute(
            "SELECT * FROM terminology_rule WHERE rule_key=?",
            (RULE_KEY,),
        ).fetchone()
        evidence = {
            "source": "rule-agent-evaluation-replay",
            "previousProposalId": proposal["proposal_id"],
            "previousReplayId": proposal["evaluation_replay_id"],
            "evaluationCaseIds": case_ids,
            "sourceTerm": "KV",
            "targetTerm": "kV",
            "guardrail": "only change unit capitalization; preserve all other characters",
        }
        if term is None:
            connection.execute(
                """
                INSERT INTO terminology_rule
                  (term_rule_id,rule_key,rule_type,source_term,target_term,context_condition_json,
                   priority,status,version,evidence_json,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    "term-format-unit-uppercase-kv-to-kv",
                    RULE_KEY,
                    "unit",
                    "KV",
                    "kV",
                    "{}",
                    100,
                    "candidate",
                    RULE_VERSION,
                    json.dumps(evidence, ensure_ascii=False),
                    now,
                    now,
                ),
            )
        elif term["status"] != "active":
            connection.execute(
                """
                UPDATE terminology_rule
                SET source_term='KV',target_term='kV',status='candidate',version=?,evidence_json=?,updated_at=?
                WHERE rule_key=?
                """,
                (RULE_VERSION, json.dumps(evidence, ensure_ascii=False), now, RULE_KEY),
            )

        connection.execute(
            """
            UPDATE rule_agent_proposal
            SET discovery_filter_status='filtered',
                discovery_filter_reason='superseded_by_kv_standard_change',
                discovery_filtered_at=?,updated_at=?
            WHERE proposal_id=?
            """,
            (now, now, proposal["proposal_id"]),
        )
        connection.execute(
            """
            INSERT INTO audit_event(entity_type,entity_id,event_type,actor,payload_json,event_at)
            VALUES (?,?,?,?,?,?)
            """,
            (
                "semantic_standard",
                RULE_KEY,
                "kv_standard_baseline_updated",
                ACTOR,
                json.dumps(
                    {
                        "ruleKey": RULE_KEY,
                        "ruleVersion": RULE_VERSION,
                        "updatedCaseCount": len(updated_cases),
                        "cases": updated_cases,
                        "sourceWrite": False,
                        "formalPublication": False,
                    },
                    ensure_ascii=False,
                ),
                now,
            ),
        )
        connection.commit()
        print(
            json.dumps(
                {
                    "status": "updated_for_replay",
                    "ruleKey": RULE_KEY,
                    "ruleVersion": RULE_VERSION,
                    "updatedCaseCount": len(updated_cases),
                    "cases": updated_cases,
                    "sourceWrite": False,
                    "formalPublication": False,
                },
                ensure_ascii=False,
            )
        )
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()


if __name__ == "__main__":
    main()
