"""Promote confirmed sample review outcomes into replay cases and rule candidates."""
from __future__ import annotations

import json
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent
SQLITE_DB = ROOT / "data" / "semantic_workflow.sqlite3"
RULE_VERSION = "sample-learned-20260812-v1"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def main() -> None:
    connection = sqlite3.connect(SQLITE_DB)
    connection.row_factory = sqlite3.Row
    sample = connection.execute(
        "SELECT * FROM review_sample ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    if sample is None:
        raise SystemExit("No review sample exists.")

    rows = connection.execute(
        """
        SELECT r.review_id,r.candidate_id,r.decision,r.reviewed_description,
          r.review_note,r.reviewer,r.reviewed_at,
          c.original_description,c.candidate_description,c.rule_version,
          c.validator_version,c.reason_codes_json,
          d.source_snapshot_id,d.source_schema,d.site_id,d.asset_number,
          d.location_code,d.location_description,d.location_parent,
          d.classification_description,d.source_row_hash,d.context_hash
        FROM review_sample_item i
        JOIN review_decision r ON r.candidate_id=i.candidate_id
        JOIN semantic_candidate c ON c.candidate_id=r.candidate_id
        JOIN device_identity d ON d.device_id=c.device_id
        WHERE i.sample_id=?
        ORDER BY r.reviewed_at,r.review_id
        """,
        (sample["sample_id"],),
    ).fetchall()
    if not rows:
        raise SystemExit("The review sample has no completed decisions.")

    changed = [
        row for row in rows
        if row["decision"] in {"approved", "modified"}
        and row["original_description"].strip()
        != (row["reviewed_description"] or row["candidate_description"] or "").strip()
    ]
    rule_examples = {
        "format.fullwidth_parenthesis_to_ascii": [
            row for row in changed
            if any(mark in (row["original_description"] or "") for mark in ("（", "）"))
        ],
        "format.fullwidth_comma_to_ascii": [
            row for row in changed
            if "，" in (row["original_description"] or "")
        ],
    }

    now = utc_now()
    connection.execute("BEGIN IMMEDIATE")
    evaluation_case_ids: list[str] = []
    for row in rows:
        final_description = (row["reviewed_description"] or row["candidate_description"] or "").strip()
        case_id = f"case-review-{row['review_id']}"
        failure_type = (
            "manual_normalization"
            if row["decision"] in {"approved", "modified"}
            and row["original_description"].strip() != final_description
            else "manual_rejection"
            if row["decision"] == "rejected"
            else "manual_confirmation"
        )
        context = {
            "location_code": row["location_code"] or "",
            "location_description": row["location_description"] or "",
            "location_parent": row["location_parent"] or "",
            "classification_description": row["classification_description"] or "",
            "source_row_hash": row["source_row_hash"],
            "context_hash": row["context_hash"],
            "reason_codes": json.loads(row["reason_codes_json"] or "[]"),
            "reviewer": row["reviewer"],
            "review_note": row["review_note"] or "",
        }
        connection.execute(
            """
            INSERT OR IGNORE INTO evaluation_case
              (case_id,source_review_id,source_snapshot_id,source_schema,site_id,
               asset_number,input_description,context_json,expected_decision,
               expected_description,failure_type,active,introduced_rule_version,created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                case_id,
                row["review_id"],
                row["source_snapshot_id"],
                row["source_schema"],
                row["site_id"],
                row["asset_number"],
                row["original_description"],
                json.dumps(context, ensure_ascii=False),
                row["decision"],
                final_description if row["decision"] in {"approved", "modified"} else None,
                failure_type,
                1,
                RULE_VERSION,
                now,
            ),
        )
        evaluation_case_ids.append(case_id)

    created_rules: list[str] = []
    for rule_key, examples in rule_examples.items():
        if not examples:
            continue
        if rule_key.endswith("parenthesis_to_ascii"):
            source_term, target_term = "（/）", "(/)"
        else:
            source_term, target_term = "，", ","
        evidence = {
            "sample_id": sample["sample_id"],
            "source_review_ids": [row["review_id"] for row in examples],
            "example_count": len(examples),
            "examples": [
                {
                    "candidate_id": row["candidate_id"],
                    "site_id": row["site_id"],
                    "asset_number": row["asset_number"],
                    "original": row["original_description"],
                    "approved": row["reviewed_description"] or row["candidate_description"],
                }
                for row in examples
            ],
        }
        connection.execute(
            """
            INSERT OR IGNORE INTO terminology_rule
              (term_rule_id,rule_key,rule_type,source_term,target_term,
               site_scope,classification_scope,context_condition_json,priority,
               status,version,evidence_json,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                f"term-{rule_key.replace('.', '-')}",
                rule_key,
                "format",
                source_term,
                target_term,
                None,
                None,
                json.dumps({
                    "field": "DESCRIPTION",
                    "guardrails": [
                        "只做标点格式归一，不改变设备语义",
                        "保留原始描述并记录候选哈希",
                        "启用前必须通过全部活动回放案例",
                    ],
                }, ensure_ascii=False),
                50,
                "candidate",
                RULE_VERSION,
                json.dumps(evidence, ensure_ascii=False),
                now,
                now,
            ),
        )
        created_rules.append(rule_key)

    audit_id = f"sample-learning-{sample['sample_id']}"
    existing_audit = connection.execute(
        "SELECT 1 FROM audit_event WHERE entity_type='sample_learning' AND entity_id=? LIMIT 1",
        (audit_id,),
    ).fetchone()
    if existing_audit is None:
        connection.execute(
            """
            INSERT INTO audit_event(entity_type,entity_id,event_type,actor,payload_json,event_at)
            VALUES (?,?,?,?,?,?)
            """,
            (
                "sample_learning",
                audit_id,
                "sample_reviews_promoted",
                "promote_sample_reviews.py",
                json.dumps({
                    "sample_id": sample["sample_id"],
                    "evaluation_case_count": len(evaluation_case_ids),
                    "rule_candidates": created_rules,
                    "changed_count": len(changed),
                    "decision_counts": dict(Counter(row["decision"] for row in rows)),
                }, ensure_ascii=False),
                now,
            ),
        )
    connection.commit()

    print(json.dumps({
        "sample_id": sample["sample_id"],
        "review_count": len(rows),
        "changed_count": len(changed),
        "evaluation_case_count": len(evaluation_case_ids),
        "rule_candidates": created_rules,
        "decision_counts": dict(Counter(row["decision"] for row in rows)),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
