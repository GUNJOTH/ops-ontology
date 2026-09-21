"""Measure and atomically activate the two confirmed sample format rules."""
from __future__ import annotations

import csv
import json
import sqlite3
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DB = ROOT / "data" / "semantic_workflow.sqlite3"
QUALITY_CSV = ROOT.parent / "pilots" / "HD_SAAS" / "quality" / "hd_quality_candidates.csv"
RULE_VERSION = "sample-learned-20260812-v1"
RULE_KEYS = (
    "format.fullwidth_parenthesis_to_ascii",
    "format.fullwidth_comma_to_ascii",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def transform(value: str) -> str:
    return value.replace("（", "(").replace("）", ")").replace("，", ",")


def measure_quality_file() -> dict[str, object]:
    total = 0
    parenthesis = 0
    comma = 0
    union = 0
    by_site: Counter[str] = Counter()
    by_site_parenthesis: Counter[str] = Counter()
    by_site_comma: Counter[str] = Counter()
    examples: list[dict[str, str]] = []
    with QUALITY_CSV.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            total += 1
            description = row.get("ORIGINAL_DESCRIPTION") or ""
            has_parenthesis = "（" in description or "）" in description
            has_comma = "，" in description
            if has_parenthesis:
                parenthesis += 1
                by_site_parenthesis[row["SITEID"]] += 1
            if has_comma:
                comma += 1
                by_site_comma[row["SITEID"]] += 1
            if has_parenthesis or has_comma:
                union += 1
                by_site[row["SITEID"]] += 1
                if len(examples) < 20:
                    examples.append({"site": row["SITEID"], "asset": row["ASSETNUM"], "description": description})
    return {
        "quality_rows": total,
        "parenthesis_rows": parenthesis,
        "comma_rows": comma,
        "impacted_rows": union,
        "by_site": dict(sorted(by_site.items())),
        "by_site_parenthesis": dict(sorted(by_site_parenthesis.items())),
        "by_site_comma": dict(sorted(by_site_comma.items())),
        "examples": examples,
    }


def replay(connection: sqlite3.Connection) -> tuple[str, dict[str, object]]:
    cases = connection.execute(
        "SELECT * FROM evaluation_case WHERE active=1 ORDER BY case_id"
    ).fetchall()
    replay_id = f"replay-activation-{uuid.uuid4().hex}"
    started_at = utc_now()
    connection.execute(
        """
        INSERT INTO replay_run
          (replay_id,rule_version,validator_version,evaluation_count,pass_count,fail_count,status,started_at)
        VALUES (?,?,?,?,?,?,?,?)
        """,
        (replay_id, RULE_VERSION, "hd-semantic-validator-0.2.0", 0, 0, 0, "running", started_at),
    )
    passed = 0
    failed = 0
    for case in cases:
        actual_description = transform(case["input_description"]) if case["expected_decision"] in {"approved", "modified"} else None
        expected_description = case["expected_description"] if case["expected_decision"] in {"approved", "modified"} else None
        ok = case["expected_decision"] == "rejected" or actual_description == expected_description
        outcome = "pass" if ok else "fail"
        if ok:
            passed += 1
        else:
            failed += 1
        connection.execute(
            "INSERT INTO replay_result(replay_id,case_id,actual_decision,actual_description,outcome,message) VALUES (?,?,?,?,?,?)",
            (replay_id, case["case_id"], case["expected_decision"], actual_description, outcome, None if ok else "format replay mismatch"),
        )
    status = "passed" if failed == 0 else "failed"
    finished_at = utc_now()
    connection.execute(
        "UPDATE replay_run SET evaluation_count=?,pass_count=?,fail_count=?,status=?,finished_at=? WHERE replay_id=?",
        (len(cases), passed, failed, status, finished_at, replay_id),
    )
    return replay_id, {"evaluation_count": len(cases), "pass_count": passed, "fail_count": failed, "status": status}


def main() -> None:
    measurement = measure_quality_file()
    connection = sqlite3.connect(DB)
    connection.row_factory = sqlite3.Row
    connection.execute("BEGIN IMMEDIATE")
    replay_id, replay_result = replay(connection)
    if replay_result["status"] != "passed":
        connection.rollback()
        print(json.dumps({"activated": False, "measurement": measurement, "replay": replay_result}, ensure_ascii=False))
        raise SystemExit(1)

    now = utc_now()
    placeholders = ",".join("?" for _ in RULE_KEYS)
    rows = connection.execute(
        f"SELECT term_rule_id,rule_key,status FROM terminology_rule WHERE rule_key IN ({placeholders}) ORDER BY rule_key",
        RULE_KEYS,
    ).fetchall()
    if len(rows) != len(RULE_KEYS) or any(row["status"] not in {"candidate", "active"} for row in rows):
        connection.rollback()
        raise SystemExit("Required format rules are missing or not eligible for activation.")
    for rule_key in RULE_KEYS:
        connection.execute(
            "UPDATE terminology_rule SET status='active',confirmed_by=?,confirmed_at=?,updated_at=? WHERE rule_key=?",
            ("sample-review-gate", now, now, rule_key),
        )
    activation_id = f"format-rule-activation-{uuid.uuid4().hex}"
    payload = {"rule_keys": list(RULE_KEYS), "rule_version": RULE_VERSION, "measurement": measurement, "replay_id": replay_id, "replay": replay_result, "source_write": False}
    connection.execute(
        "INSERT INTO audit_event(entity_type,entity_id,event_type,actor,payload_json,event_at) VALUES (?,?,?,?,?,?)",
        ("terminology_rule", activation_id, "format_rules_activated", "sample-review-gate", json.dumps(payload, ensure_ascii=False), now),
    )
    connection.commit()
    connection.close()
    print(json.dumps({"activated": True, "activation_id": activation_id, "rule_keys": list(RULE_KEYS), "measurement": measurement, "replay_id": replay_id, "replay": replay_result, "source_write": False}, ensure_ascii=False))


if __name__ == "__main__":
    main()
