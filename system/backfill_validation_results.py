"""Backfill auditable validation details for the current device candidates.

This is a local validation-only operation. It reads the semantic candidate and
device snapshot tables, writes validation_result rows and one audit event, and
never changes source data, review decisions, candidate state, or publication.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent
SQLITE_DB = ROOT / "data" / "semantic_workflow.sqlite3"
VALIDATOR_VERSION = "hd-semantic-validator-0.3.1"
VALIDATOR_TYPES = ("contract", "identity", "evidence", "semantic", "consistency")
VALID_EVIDENCE_LEVELS = {"basic", "strong", "source_preview_and_replay"}
BATCH_SIZE = 2_000


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def is_valid_description(value: str) -> bool:
    return bool(value and value.strip()) and len(value) <= 2_000 and not any(
        ord(char) < 32 and char not in "\t\r\n" for char in value
    )


def has_semantic_text(value: str) -> bool:
    return bool(re.search(r"[A-Za-z0-9_一-龥]", value or ""))


def parse_reason_codes(value: str) -> tuple[list[str], bool]:
    try:
        parsed = json.loads(value or "[]")
    except json.JSONDecodeError:
        return [], False
    return parsed, isinstance(parsed, list) and all(isinstance(item, str) for item in parsed)


def validation_row(
    candidate_id: str,
    validator_type: str,
    passed: bool,
    reason_code: str,
    message: str,
    evidence: dict[str, object],
    validated_at: str,
) -> tuple[object, ...]:
    return (
        candidate_id,
        validator_type,
        "info" if passed else "hard_failure",
        "pass" if passed else "fail",
        reason_code,
        message,
        json.dumps(evidence, ensure_ascii=False, sort_keys=True),
        VALIDATOR_VERSION,
        validated_at,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch-id", help="只补齐指定批次；默认使用 SQLite 最新批次")
    args = parser.parse_args()
    connection = sqlite3.connect(SQLITE_DB, timeout=60)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=60000")
    batch = (
        connection.execute("SELECT * FROM batch_run WHERE batch_id=?", (args.batch_id,)).fetchone()
        if args.batch_id
        else connection.execute("SELECT * FROM batch_run ORDER BY started_at DESC LIMIT 1").fetchone()
    )
    if not batch:
        raise SystemExit("SQLite 中没有可用批次")
    batch_id = batch["batch_id"]
    source_snapshot_id = batch["source_snapshot_id"]

    duplicate_identity_keys = {
        tuple(row)
        for row in connection.execute(
            """
            SELECT source_snapshot_id,source_schema,site_id,asset_number
            FROM device_identity
            GROUP BY source_snapshot_id,source_schema,site_id,asset_number
            HAVING count(*) > 1
            """
        )
    }
    candidates_query = """
        SELECT
          c.candidate_id,c.original_description,c.candidate_description,
          c.validator_status,c.reason_codes_json,c.evidence_level,
          c.rule_version,c.validator_version,c.candidate_hash,
          d.source_snapshot_id,d.source_schema,d.site_id,d.asset_number,
          d.source_row_hash,d.context_hash,d.location_code,
          d.location_description,d.location_parent,d.classification_description
        FROM semantic_candidate c
        JOIN device_identity d ON d.device_id=c.device_id
        WHERE c.batch_id=?
        ORDER BY c.candidate_id
    """
    candidates = connection.execute(candidates_query, (batch_id,))

    started_at = utc_now()
    result_count = 0
    fail_count = 0
    candidate_count = 0
    pending_rows: list[tuple[object, ...]] = []

    upsert_sql = """
        INSERT INTO validation_result
          (candidate_id,validator_type,severity,outcome,reason_code,message,evidence_json,validator_version,validated_at)
        VALUES (?,?,?,?,?,?,?,?,?)
        ON CONFLICT(candidate_id,validator_type,reason_code) DO UPDATE SET
          severity=excluded.severity,
          outcome=excluded.outcome,
          message=excluded.message,
          evidence_json=excluded.evidence_json,
          validator_version=excluded.validator_version,
          validated_at=excluded.validated_at
    """

    connection.execute("BEGIN")
    for candidate in candidates:
        candidate_count += 1
        candidate_id = candidate["candidate_id"]
        description = candidate["candidate_description"] or ""
        reason_codes, reason_codes_valid = parse_reason_codes(candidate["reason_codes_json"])
        identity_key = (
            candidate["source_snapshot_id"],
            candidate["source_schema"],
            candidate["site_id"],
            candidate["asset_number"],
        )
        identity_present = all(
            candidate[field] not in (None, "")
            for field in ("source_schema", "site_id", "asset_number")
        )
        provenance_present = all(
            candidate[field] not in (None, "")
            for field in (
                "source_row_hash",
                "context_hash",
                "candidate_hash",
                "rule_version",
                "validator_version",
            )
        )
        semantic_present = has_semantic_text(description)
        consistency_ok = (
            candidate["validator_status"] in {"candidate", "needs_review", "blocked"}
            and candidate["evidence_level"] in VALID_EVIDENCE_LEVELS
            and reason_codes_valid
            and not any("CONFLICT" in code or "BLOCK" in code for code in reason_codes)
        )
        validated_at = utc_now()
        rows = [
            validation_row(
                candidate_id,
                "contract",
                is_valid_description(description),
                "DESCRIPTION_CONTRACT_VALID",
                "候选描述满足非空、长度和控制字符约束。" if is_valid_description(description) else "候选描述未通过合同约束。",
                {"length": len(description), "max_length": 2_000, "control_characters": False},
                validated_at,
            ),
            validation_row(
                candidate_id,
                "identity",
                identity_present and identity_key not in duplicate_identity_keys,
                "IDENTITY_KEY_UNIQUE",
                "SITEID + ASSETNUM 身份键完整且在当前源快照内唯一。" if identity_present and identity_key not in duplicate_identity_keys else "设备身份键缺失或重复。",
                {
                    "source_schema": candidate["source_schema"],
                    "site_id": candidate["site_id"],
                    "asset_number": candidate["asset_number"],
                    "unique_in_snapshot": identity_key not in duplicate_identity_keys,
                },
                validated_at,
            ),
            validation_row(
                candidate_id,
                "evidence",
                provenance_present and reason_codes_valid,
                "PROVENANCE_COMPLETE",
                "来源哈希、上下文哈希、候选哈希、规则版本和校验器版本齐全。" if provenance_present and reason_codes_valid else "候选缺少可追溯证据或原因码格式无效。",
                {
                    "source_row_hash_present": bool(candidate["source_row_hash"]),
                    "context_hash_present": bool(candidate["context_hash"]),
                    "candidate_hash_present": bool(candidate["candidate_hash"]),
                    "rule_version": candidate["rule_version"],
                    "validator_version": candidate["validator_version"],
                    "reason_codes_valid": reason_codes_valid,
                },
                validated_at,
            ),
            validation_row(
                candidate_id,
                "semantic",
                semantic_present,
                "SEMANTIC_DESCRIPTION_PRESENT",
                "候选描述包含可读的中文、英文、数字或下划线内容。" if semantic_present else "候选描述疑似仅包含符号，需复核。",
                {"length": len(description), "has_readable_text": semantic_present},
                validated_at,
            ),
            validation_row(
                candidate_id,
                "consistency",
                consistency_ok,
                "CONTEXT_NO_BLOCKING_CONFLICT",
                "候选状态、证据等级和原因码没有发现阻断性冲突。" if consistency_ok else "候选状态、证据等级或原因码存在阻断性冲突。",
                {
                    "validator_status": candidate["validator_status"],
                    "evidence_level": candidate["evidence_level"],
                    "reason_codes": reason_codes,
                    "location_present": bool(candidate["location_code"] or candidate["location_description"]),
                    "classification_present": bool(candidate["classification_description"]),
                    "parent_location_present": bool(candidate["location_parent"]),
                },
                validated_at,
            ),
        ]
        pending_rows.extend(rows)
        result_count += len(rows)
        fail_count += sum(row[3] == "fail" for row in rows)
        if len(pending_rows) >= BATCH_SIZE * len(VALIDATOR_TYPES):
            connection.executemany(upsert_sql, pending_rows)
            pending_rows.clear()

    if pending_rows:
        connection.executemany(upsert_sql, pending_rows)

    connection.execute(
        """
        INSERT INTO audit_event(entity_type,entity_id,event_type,actor,payload_json,event_at)
        VALUES (?,?,?,?,?,?)
        """,
        (
            "validation",
            f"validation-backfill-{started_at}",
            "validation_backfill_completed",
            "backfill_validation_results.py",
            json.dumps(
                {
                    "batch_id": batch_id,
                    "source_snapshot_id": source_snapshot_id,
                    "candidate_count": candidate_count,
                    "result_count": result_count,
                    "fail_count": fail_count,
                    "validator_types": VALIDATOR_TYPES,
                    "validator_version": VALIDATOR_VERSION,
                    "source_write": False,
                    "formal_publication": False,
                },
                ensure_ascii=False,
            ),
            utc_now(),
        ),
    )
    connection.commit()

    persisted_count = connection.execute(
        """
        SELECT count(*)
        FROM validation_result v
        JOIN semantic_candidate c ON c.candidate_id=v.candidate_id
        WHERE c.batch_id=?
        """,
        (batch_id,),
    ).fetchone()[0]
    distinct_candidates = connection.execute(
        """
        SELECT count(DISTINCT v.candidate_id)
        FROM validation_result v
        JOIN semantic_candidate c ON c.candidate_id=v.candidate_id
        WHERE c.batch_id=?
        """,
        (batch_id,),
    ).fetchone()[0]
    connection.close()
    print(
        json.dumps(
            {
                "batch_id": batch_id,
                "source_snapshot_id": source_snapshot_id,
                "candidate_count": candidate_count,
                "result_count": result_count,
                "persisted_validation_rows": persisted_count,
                "distinct_candidates": distinct_candidates,
                "expected_rows": candidate_count * len(VALIDATOR_TYPES),
                "fail_count": fail_count,
                "validator_version": VALIDATOR_VERSION,
                "source_write": False,
                "formal_publication": False,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
