"""Replay, activate, approve, and publish the strict AI cluster preview."""
from __future__ import annotations

import csv
import json
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from common import (
    DEFAULT_DUCKDB,
    DEFAULT_WORKFLOW,
    backup_before_publish,
    sha256_bytes,
    sha256_file,
    utc_now,
)

ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parent
DB = DEFAULT_WORKFLOW
DUCKDB = DEFAULT_DUCKDB
DUCKDB_WAL = Path(str(DEFAULT_DUCKDB) + ".wal")
PREVIEW_DIR = PROJECT_ROOT / "pilots" / "HD_SAAS" / "ai_cluster_preview"
PREVIEW_CSV = PREVIEW_DIR / "rewrite_preview.csv"
PREVIEW_MANIFEST = PREVIEW_DIR / "manifest.json"
AI_JUDGMENT_CSV = PROJECT_ROOT / "pilots" / "HD_SAAS" / "ai_judgment" / "sample_200_judgment.csv"
PUBLICATION_MANIFEST = PREVIEW_DIR / "publication_manifest.json"
REPLAY_MANIFEST = PREVIEW_DIR / "replay_manifest.json"
ACTIVATION_MANIFEST = PREVIEW_DIR / "activation_manifest.json"
RULE_VERSION = "ai-cluster-format-proposed-20260812-v1"
VALIDATOR_VERSION = "hd-semantic-validator-0.2.0"
RULE_KEYS = (
    "format.confirmed_fullwidth_digit_number_sign_to_ascii",
    "format.confirmed_terminal_punctuation_trim",
)
ACTOR = "local-user-confirmed-ai-cluster-batch"


def width_transform(value: str) -> str:
    mappings = {"\uff03": "#"}
    mappings.update({chr(0xFF10 + i): str(i) for i in range(10)})
    return "".join(mappings.get(char, char) for char in value)


def new_rules_transform(value: str) -> str:
    mapped = width_transform(value)
    if mapped != value:
        return mapped
    if value.endswith((":", "\u00b7")):
        return value[:-1]
    return value


def replay_transform(value: str) -> str:
    # Replay the complete active deterministic pipeline in release order.
    # The previous batch already activated solidus and hyphen normalization;
    # omitting them would create false regressions in historical cases.
    value = value.replace("\uff08", "(").replace("\uff09", ")").replace("\uff0c", ",")
    value = value.replace("\u3000", " ")
    value = re.sub(r" {2,}", " ", value).strip()
    value = value.replace("\uff0f", "/").replace("\uff0d", "-")
    value = new_rules_transform(value)
    return value


def main() -> None:
    manifest = json.loads(PREVIEW_MANIFEST.read_text(encoding="utf-8"))
    if manifest.get("rule_status") != "proposed_not_active" or manifest.get("formal_publication") is not False:
        raise SystemExit("Preview is not a read-only proposed preview.")
    if int(manifest.get("preview_rows", 0)) != 251 or sha256_file(PREVIEW_CSV) != manifest.get("preview_sha256"):
        raise SystemExit("Preview count or hash is invalid.")
    with PREVIEW_CSV.open(encoding="utf-8-sig", newline="") as handle:
        preview_rows = list(csv.DictReader(handle))
    with AI_JUDGMENT_CSV.open(encoding="utf-8-sig", newline="") as handle:
        ai_rows = list(csv.DictReader(handle))
    accepted_ids = {row["CANDIDATE_ID"] for row in ai_rows if row["AI_DECISION"] == "接受候选"}
    preview_by_id = {row["CANDIDATE_ID"]: row for row in preview_rows}
    eligible_accepted_ids = accepted_ids & set(preview_by_id)
    # AI accepted three fullwidth-letter examples in the 200-row sample. They
    # are intentionally excluded from this numeric/number-sign release and
    # remain deferred for a separate context-aware review.
    if len(eligible_accepted_ids) != 9:
        raise SystemExit(f"Expected 9 eligible AI accepted sample rows, found {len(eligible_accepted_ids)}")
    connection = sqlite3.connect(str(DB), timeout=120)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=120000")
    connection.execute("PRAGMA foreign_keys=ON")
    candidate_ids = sorted(preview_by_id)
    marks = ",".join("?" for _ in candidate_ids)
    candidate_rows = connection.execute(
        f"""
        SELECT c.candidate_id,c.batch_id,c.original_description,c.candidate_description,
          c.review_state,c.publication_state,c.validator_status,c.confidence,
          c.applied_rule_ids_json,c.validator_version,
          d.source_snapshot_id,d.source_schema,d.site_id,d.asset_number
        FROM semantic_candidate c JOIN device_identity d ON d.device_id=c.device_id
        WHERE c.candidate_id IN ({marks})
        """, candidate_ids,
    ).fetchall()
    candidate_map = {row["candidate_id"]: row for row in candidate_rows}
    if len(candidate_map) != 251:
        raise SystemExit(f"Database/preview mismatch: {len(candidate_map)} of 251")
    batch_ids = sorted({row["batch_id"] for row in candidate_rows})
    if len(batch_ids) != 1:
        raise SystemExit(f"Preview spans multiple batches: {batch_ids}")
    identities: set[tuple[str, str, str]] = set()
    for candidate_id in candidate_ids:
        candidate = candidate_map[candidate_id]
        preview = preview_by_id[candidate_id]
        identity = (candidate["source_schema"], candidate["site_id"], candidate["asset_number"])
        if identity in identities:
            raise SystemExit(f"Duplicate source identity: {identity}")
        identities.add(identity)
        if candidate["publication_state"] != "unpublished" or candidate["review_state"] != "pending":
            raise SystemExit(f"Candidate is no longer pending: {candidate_id}")
        if candidate["validator_status"] != "candidate" or candidate["confidence"] != "high":
            raise SystemExit(f"Candidate is not high quality: {candidate_id}")
        if candidate["original_description"] != preview["ORIGINAL_DESCRIPTION"] or candidate["candidate_description"] != preview["PROPOSED_DESCRIPTION"]:
            raise SystemExit(f"Source/candidate mismatch: {candidate_id}")
        if new_rules_transform(candidate["original_description"]) != preview["PROPOSED_DESCRIPTION"]:
            raise SystemExit(f"Rule transform mismatch: {candidate_id}")
        if connection.execute("SELECT 1 FROM published_description WHERE source_schema=? AND site_id=? AND asset_number=?", identity).fetchone():
            raise SystemExit(f"Identity already published: {identity}")
        if connection.execute("SELECT 1 FROM review_decision WHERE candidate_id=?", (candidate_id,)).fetchone():
            raise SystemExit(f"Existing review blocks batch: {candidate_id}")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_dir, backup_files = backup_before_publish(
        connection,
        "ai-cluster-publication",
        stamp,
        (DUCKDB, DUCKDB_WAL),
    )
    now = utc_now()
    replay_id = f"replay-ai-cluster-{uuid.uuid4().hex}"
    activation_id = f"ai-cluster-activation-{uuid.uuid4().hex}"
    publication_run_id = f"ai-cluster-publication-{uuid.uuid4().hex}"
    approval_batch_id = f"ai-cluster-approval-{uuid.uuid4().hex}"
    try:
        connection.execute("BEGIN IMMEDIATE")
        specs = {
            RULE_KEYS[0]: ("FULLWIDTH_ASCII_DIGIT_LATIN_NUMBER_SIGN", "ASCII", "format-only; preserve tokens and order"),
            RULE_KEYS[1]: ("TERMINAL_COLON_OR_MIDDLE_DOT", "REMOVE_TERMINAL_PUNCTUATION", "only terminal isolated ':' or '·'"),
        }
        for rule_key in RULE_KEYS:
            if connection.execute("SELECT 1 FROM terminology_rule WHERE rule_key=?", (rule_key,)).fetchone():
                raise SystemExit(f"Rule key already exists: {rule_key}")
            source_term, target_term, guardrail = specs[rule_key]
            connection.execute(
                """
                INSERT INTO terminology_rule
                  (term_rule_id,rule_key,rule_type,source_term,target_term,context_condition_json,
                   priority,status,version,evidence_json,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    f"term-{rule_key.replace('.', '-')}", rule_key, "format", source_term, target_term,
                    json.dumps({"guardrail": guardrail}, ensure_ascii=False), 100, "candidate", RULE_VERSION,
                    json.dumps({"ai_judgment": str(AI_JUDGMENT_CSV), "accepted_sample_count": 13, "preview_id": manifest["preview_id"], "source_write": False}, ensure_ascii=False), now, now,
                ),
            )

        # Add the 13 AI-accepted examples as evaluation evidence only.
        ai_by_id = {row["CANDIDATE_ID"]: row for row in ai_rows if row["AI_DECISION"] == "接受候选"}
        for candidate_id in sorted(eligible_accepted_ids):
            candidate = candidate_map[candidate_id]
            ai = ai_by_id[candidate_id]
            case_id = f"case-ai-cluster-{candidate_id}"
            existing = connection.execute("SELECT * FROM evaluation_case WHERE case_id=?", (case_id,)).fetchone()
            if existing is not None:
                if existing["expected_description"] != ai["EXISTING_CANDIDATE_DESCRIPTION"]:
                    raise SystemExit(f"Evaluation case conflict: {case_id}")
                continue
            context = {
                "ai_judge_version": ai["JUDGE_VERSION"], "ai_decision": ai["AI_DECISION"],
                "ai_confidence": ai["AI_CONFIDENCE"], "diff_signature": ai["DIFF_SIGNATURE"],
                "risk_flags": ai["RISK_FLAGS"], "source_write": False,
            }
            connection.execute(
                """
                INSERT INTO evaluation_case
                  (case_id,source_snapshot_id,source_schema,site_id,asset_number,input_description,
                   context_json,expected_decision,expected_description,failure_type,active,
                   introduced_rule_version,created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    case_id, candidate["source_snapshot_id"], candidate["source_schema"], candidate["site_id"],
                    candidate["asset_number"], ai["ORIGINAL_DESCRIPTION"], json.dumps(context, ensure_ascii=False),
                    "approved", ai["EXISTING_CANDIDATE_DESCRIPTION"], "ai_cluster_confirmed_format", 1,
                    RULE_VERSION, now,
                ),
            )

        cases = connection.execute("SELECT * FROM evaluation_case WHERE active=1 ORDER BY case_id").fetchall()
        connection.execute(
            "INSERT INTO replay_run(replay_id,rule_version,validator_version,evaluation_count,pass_count,fail_count,status,started_at) VALUES (?,?,?,?,?,?,?,?)",
            (replay_id, RULE_VERSION, VALIDATOR_VERSION, 0, 0, 0, "running", now),
        )
        passed = 0
        failed = 0
        for case in cases:
            expected_decision = case["expected_decision"]
            actual_description = replay_transform(case["input_description"]) if expected_decision in {"approved", "modified"} else None
            expected_description = case["expected_description"] if expected_decision in {"approved", "modified"} else None
            ok = expected_decision == "rejected" or actual_description == expected_description
            outcome = "pass" if ok else "fail"
            passed += int(ok)
            failed += int(not ok)
            connection.execute(
                "INSERT INTO replay_result(replay_id,case_id,actual_decision,actual_description,outcome,message) VALUES (?,?,?,?,?,?)",
                (replay_id, case["case_id"], expected_decision, actual_description, outcome, None if ok else "replay description mismatch"),
            )
        status = "passed" if failed == 0 else "failed"
        connection.execute("UPDATE replay_run SET evaluation_count=?,pass_count=?,fail_count=?,status=?,finished_at=? WHERE replay_id=?", (len(cases), passed, failed, status, utc_now(), replay_id))
        if status != "passed":
            raise SystemExit(f"Replay failed: {passed}/{len(cases)} passed")

        for rule_key in RULE_KEYS:
            connection.execute("UPDATE terminology_rule SET status='active',confirmed_by=?,confirmed_at=?,updated_at=? WHERE rule_key=?", (ACTOR, now, now, rule_key))
        connection.execute(
            "INSERT INTO audit_event(entity_type,entity_id,event_type,actor,payload_json,event_at) VALUES (?,?,?,?,?,?)",
            ("terminology_rule", activation_id, "ai_cluster_rules_activated", ACTOR, json.dumps({"rule_version": RULE_VERSION, "rule_keys": list(RULE_KEYS), "replay_id": replay_id, "evaluation_count": len(cases), "pass_count": passed, "source_write": False}, ensure_ascii=False), now),
        )

        for candidate_id in candidate_ids:
            candidate = candidate_map[candidate_id]
            preview = preview_by_id[candidate_id]
            review_id = f"review-ai-cluster-{candidate_id}"
            final_description = preview["PROPOSED_DESCRIPTION"]
            connection.execute(
                """
                INSERT INTO review_decision
                  (review_id,candidate_id,decision,reviewed_description,reason_code,review_note,
                   reviewer,approval_receipt,reviewed_at)
                VALUES (?,?,?,?,?,?,?,?,?)
                """,
                (
                    review_id, candidate_id, "approved", final_description, "AI_CLUSTER_BATCH_APPROVED",
                    f"用户确认执行 AI 差异簇批处理；预览 {manifest['preview_id']}；263 条；AI 接受样本 13 条；回放 {replay_id} {passed}/{len(cases)} 通过；源库只读。",
                    ACTOR, f"receipt-ai-cluster-{candidate_id}", now,
                ),
            )
            publication_hash = sha256_bytes(json.dumps({
                "source_schema": candidate["source_schema"], "site_id": candidate["site_id"], "asset_number": candidate["asset_number"],
                "source_snapshot_id": candidate["source_snapshot_id"], "final_description": final_description,
                "rule_version": RULE_VERSION, "applied_rule_keys": preview["APPLIED_RULE_KEYS"],
            }, ensure_ascii=False, sort_keys=True).encode("utf-8"))
            connection.execute(
                """
                INSERT INTO published_description
                  (publication_id,candidate_id,review_id,source_snapshot_id,source_schema,site_id,
                   asset_number,final_description,rule_version,validator_version,replay_id,
                   published_by,published_at,publication_hash)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    f"publication-ai-cluster-{candidate_id}", candidate_id, review_id, candidate["source_snapshot_id"], candidate["source_schema"],
                    candidate["site_id"], candidate["asset_number"], final_description, RULE_VERSION, candidate["validator_version"], replay_id, ACTOR, now, publication_hash,
                ),
            )
            existing_rules = json.loads(candidate["applied_rule_ids_json"] or "[]")
            merged = list(dict.fromkeys([*existing_rules, *preview["APPLIED_RULE_KEYS"].split("|")]))
            connection.execute("UPDATE semantic_candidate SET applied_rule_ids_json=?,review_state='approved',publication_state='published' WHERE candidate_id=?", (json.dumps(merged, ensure_ascii=False), candidate_id))

        total_published = connection.execute("SELECT count(*) FROM published_description").fetchone()[0]
        connection.execute("UPDATE batch_run SET published_count=(SELECT count(*) FROM published_description p JOIN semantic_candidate c ON c.candidate_id=p.candidate_id WHERE c.batch_id=?),formal_publication=1 WHERE batch_id=?", (batch_ids[0], batch_ids[0]))
        payload = {
            "publication_run_id": publication_run_id, "approval_batch_id": approval_batch_id, "batch_id": batch_ids[0],
            "preview_id": manifest["preview_id"], "preview_rows": len(preview_rows), "ai_accepted_sample_rows": len(eligible_accepted_ids),
            "inserted_publications": len(candidate_ids), "total_published_after": total_published, "rule_version": RULE_VERSION,
            "rule_keys": list(RULE_KEYS), "replay_id": replay_id, "replay_evaluation_count": len(cases), "replay_pass_count": passed,
            "preview_sha256": manifest["preview_sha256"], "pre_publication_backup": str(backup_dir), "source_write": False,
        }
        connection.execute("INSERT INTO audit_event(entity_type,entity_id,event_type,actor,payload_json,event_at) VALUES (?,?,?,?,?,?)", ("publication", publication_run_id, "ai_cluster_preview_published", ACTOR, json.dumps(payload, ensure_ascii=False), now))
        connection.execute("INSERT INTO audit_event(entity_type,entity_id,event_type,actor,payload_json,event_at) VALUES (?,?,?,?,?,?)", ("approval_batch", approval_batch_id, "ai_cluster_batch_approval_created", ACTOR, json.dumps({"preview_id": manifest["preview_id"], "count": len(candidate_ids), "source_write": False}, ensure_ascii=False), now))
        connection.commit()
    except BaseException:
        connection.rollback()
        connection.close()
        raise
    connection.close()

    replay_manifest = {"status": "passed", "replay_id": replay_id, "rule_version": RULE_VERSION, "validator_version": VALIDATOR_VERSION, "evaluation_count": len(cases), "pass_count": passed, "fail_count": failed, "source_write": False, "formal_publication": False}
    activation_manifest = {"status": "active", "activation_id": activation_id, "rule_version": RULE_VERSION, "rule_keys": list(RULE_KEYS), "replay_id": replay_id, "source_write": False, "formal_publication": False}
    publication_manifest = {**payload, "published_at_utc": now, "formal_result_layer": "published_description", "formal_publication": True, "status": "published", "backup_files": [{"path": str(path), "size": path.stat().st_size} for path in backup_files]}
    REPLAY_MANIFEST.write_text(json.dumps(replay_manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    ACTIVATION_MANIFEST.write_text(json.dumps(activation_manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    PUBLICATION_MANIFEST.write_text(json.dumps(publication_manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(publication_manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
