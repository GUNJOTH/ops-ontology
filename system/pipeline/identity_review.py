"""Read-only triage for unresolved identity assertions."""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import sqlite3
from collections import Counter
from datetime import datetime, timezone


def _conflict_ids(connection: sqlite3.Connection) -> set[str]:
    conflict_ids: set[str] = set()
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    if "semantic_identity_conflict" not in tables:
        return conflict_ids
    for row in connection.execute("SELECT assertion_ids_json FROM semantic_identity_conflict WHERE status='isolated'").fetchall():
        try:
            conflict_ids.update(str(item) for item in json.loads(row[0] or "[]"))
        except json.JSONDecodeError:
            continue
    return conflict_ids


def _pending_rows(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    return connection.execute(
        """
        SELECT r.review_id,r.assertion_id,r.queue_status,a.source_system,a.source_schema,a.source_table,
               a.source_key_type,a.source_key,a.canonical_object_type,a.canonical_object_id,a.confidence,
               a.review_required,a.source_snapshot_id,a.evidence_json
          FROM semantic_identity_review r
          JOIN semantic_identity_assertion a ON a.assertion_id=r.assertion_id
         WHERE r.queue_status='pending'
         ORDER BY r.created_at,r.review_id
        """
    ).fetchall()


def _classify_row(row: sqlite3.Row, conflict_ids: set[str]) -> tuple[str, str]:
    evidence = str(row["evidence_json"] or "").strip()
    raw_key_type = str(row["source_key_type"] or "").strip().upper()
    key_type = raw_key_type.replace("_", "")
    non_device_key_types = {"WONUM", "TICKETID", "C_FAULTID", "XJJLID", "WORKORDER", "DEFECTID", "FAULTID"}
    normalized_non_device = {item.replace("_", "") for item in non_device_key_types}
    if ":" in str(row["source_key"] or "") and key_type not in normalized_non_device:
        key_type = str(row["source_key"]).split(":", 1)[0].strip().upper().replace("_", "")
    if key_type in normalized_non_device:
        return "non_device_business_record", "从设备身份队列隔离，转入业务事件/业务记录关系映射"
    if str(row["assertion_id"]) in conflict_ids:
        return "isolated_conflict", "补齐同组互斥映射证据后人工处理"
    if not str(row["source_snapshot_id"] or "").strip() or not evidence:
        return "missing_provenance", "补齐 source_snapshot_id 和来源记录证据"
    if float(row["confidence"] or 0) < 0.9:
        return "low_confidence", "人工核对 KKS、位置、业务编号或关系表"
    return "reviewable_with_evidence", "人工确认；系统不自动合并"


def triage_identity_reviews(connection: sqlite3.Connection) -> dict[str, object]:
    """Classify pending assertions; never approve or merge them."""
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    if "semantic_identity_review" not in tables:
        return {"schemaVersion": "identity-review-triage-v1", "status": "not_initialized", "total": 0, "sourceWrite": False, "formalPublication": False}
    conflict_ids = _conflict_ids(connection)
    rows = _pending_rows(connection)
    categories: Counter[str] = Counter()
    namespaces: Counter[str] = Counter()
    items: list[dict[str, object]] = []
    for row in rows:
        assertion_id = str(row["assertion_id"])
        raw_key_type = str(row["source_key_type"] or "").strip().upper()
        category, next_step = _classify_row(row, conflict_ids)
        evidence = str(row["evidence_json"] or "").strip()
        categories[category] += 1
        namespaces[str(row["source_system"] or row["source_schema"] or "unknown")] += 1
        if len(items) < 200:
            items.append({"reviewId": row["review_id"], "assertionId": assertion_id, "category": category, "nextStep": next_step, "sourceSystem": row["source_system"], "sourceKey": row["source_key"], "sourceKeyType": raw_key_type, "canonicalObjectId": row["canonical_object_id"], "confidence": row["confidence"], "hasEvidence": bool(evidence), "hasSnapshot": bool(str(row["source_snapshot_id"] or "").strip()), "autoAction": "none"})
    return {
        "schemaVersion": "identity-review-triage-v1",
        "status": "completed",
        "total": len(rows),
        "categories": [{"category": key, "count": value} for key, value in sorted(categories.items())],
        "sourceSystems": [{"value": key, "count": value} for key, value in sorted(namespaces.items())],
        "sample": items,
        "policy": ["不自动合并跨系统身份", "不修改源系统或源表", "没有证据的记录继续隔离", "人工决定必须带 evidence 和 idempotencyKey"],
        "sourceWrite": False,
        "formalPublication": False,
    }


def isolate_non_device_reviews(connection: sqlite3.Connection) -> dict[str, object]:
    """Move clearly non-device keys out of the device review queue.

    This is a local queue classification, not an identity decision: the
    assertion remains ``needs_review`` and the source system is untouched.
    """
    if not {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()} >= {"semantic_identity_review", "semantic_identity_assertion"}:
        return {"status": "not_initialized", "isolatedCount": 0, "sourceWrite": False, "formalPublication": False}
    now = datetime.now(timezone.utc).isoformat()
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS semantic_identity_triage_audit(
          audit_id TEXT PRIMARY KEY, review_id TEXT NOT NULL, assertion_id TEXT NOT NULL,
          category TEXT NOT NULL, action TEXT NOT NULL, reason TEXT NOT NULL,
          idempotency_key TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL
        )
        """
    )
    conflict_ids = _conflict_ids(connection)
    isolated = 0
    for row in _pending_rows(connection):
        category, reason = _classify_row(row, conflict_ids)
        if category != "non_device_business_record":
            continue
        review_id = str(row["review_id"])
        assertion_id = str(row["assertion_id"])
        idempotency_key = f"identity-triage:non-device:{review_id}"
        audit_id = "SITA-" + hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:24]
        inserted = connection.execute(
            "INSERT OR IGNORE INTO semantic_identity_triage_audit VALUES (?,?,?,?,?,?,?,?)",
            (audit_id, review_id, assertion_id, category, "isolate_from_device_queue", reason, idempotency_key, now),
        ).rowcount
        connection.execute(
            "UPDATE semantic_identity_review SET queue_status='blocked',review_note=?,reviewed_at=?,updated_at=? WHERE review_id=? AND queue_status='pending'",
            (reason, now, now, review_id),
        )
        if inserted:
            isolated += 1
    connection.commit()
    remaining = int(connection.execute("SELECT count(*) FROM semantic_identity_review WHERE queue_status='pending'").fetchone()[0])
    return {
        "schemaVersion": "identity-review-isolation-v1",
        "status": "completed",
        "isolatedCount": isolated,
        "remainingPending": remaining,
        "policy": ["仅隔离明确的工单/缺陷/事件业务键", "身份断言仍保持 needs_review", "不自动合并、不修改源系统"],
        "sourceWrite": False,
        "formalPublication": False,
    }


def write_identity_triage_report(connection: sqlite3.Connection) -> dict[str, object]:
    payload = triage_identity_reviews(connection)
    output = pathlib.Path(__file__).resolve().parents[2] / "reports" / "identity-review"
    output.mkdir(parents=True, exist_ok=True)
    path = output / f"triage-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return {**payload, "reportPath": str(path)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Triage unresolved identity reviews without making decisions")
    parser.add_argument("--db", default=str(pathlib.Path(__file__).resolve().parents[1] / "data" / "unified_semantics.sqlite3"))
    parser.add_argument("--write-report", action="store_true")
    parser.add_argument("--isolate-non-device", action="store_true")
    args = parser.parse_args()
    connection = sqlite3.connect(args.db)
    connection.row_factory = sqlite3.Row
    try:
        if args.isolate_non_device:
            payload = isolate_non_device_reviews(connection)
            if args.write_report:
                payload = {**payload, "triage": write_identity_triage_report(connection)}
        else:
            payload = write_identity_triage_report(connection) if args.write_report else triage_identity_reviews(connection)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    finally:
        connection.close()
