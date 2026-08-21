"""Review-sample and latest-batch query helpers.

These helpers are shared by candidate, AI review, dashboard and approval flows.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from fastapi import HTTPException

from app.core.utils import utc_now

DEFAULT_SAMPLE_TARGET = 300
MAX_SAMPLE_TARGET = 5000

def latest_batch(connection: sqlite3.Connection) -> sqlite3.Row:
    row = connection.execute("SELECT * FROM batch_run ORDER BY started_at DESC LIMIT 1").fetchone()
    if row is None:
        raise HTTPException(status_code=503, detail="SQLite 中没有可用批次")
    return row


def ensure_review_sample(connection: sqlite3.Connection, batch: sqlite3.Row, target_count: int = DEFAULT_SAMPLE_TARGET) -> sqlite3.Row:
    """Create one deterministic, stratified sample for the latest batch."""
    if target_count < 1:
        raise HTTPException(status_code=400, detail="sample_size must be positive")
    sample_name = f"high-quality-{target_count}-v1"
    existing = connection.execute(
        "SELECT * FROM review_sample WHERE batch_id=? AND sample_name=?",
        (batch["batch_id"], sample_name),
    ).fetchone()
    if existing is not None:
        return existing

    rows = connection.execute(
        """
        SELECT c.candidate_id, d.site_id,
          COALESCE(NULLIF(trim(d.classification_description), ''), '未分类') AS classification,
          d.asset_number
        FROM semantic_candidate c
        JOIN device_identity d ON d.device_id=c.device_id
        WHERE c.batch_id=? AND c.validator_status='candidate' AND c.review_state='pending'
        ORDER BY d.site_id, classification, d.asset_number, c.candidate_id
        """,
        (batch["batch_id"],),
    ).fetchall()
    strata: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        key = f"{row['site_id']} / {row['classification']}"
        strata.setdefault(key, []).append(row)

    selected: list[tuple[sqlite3.Row, str]] = []
    ordered_strata = sorted(strata)
    while len(selected) < target_count:
        progressed = False
        for stratum in ordered_strata:
            bucket = strata[stratum]
            if bucket:
                selected.append((bucket.pop(0), stratum))
                progressed = True
                if len(selected) == target_count:
                    break
        if not progressed:
            break

    now = utc_now()
    sample_id = f"sample-{batch['batch_id']}-{sample_name}"
    strategy = "round_robin_by_SITEID_and_CLASSIFICATION_DESCRIPTION"
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            INSERT INTO review_sample
              (sample_id,batch_id,source_snapshot_id,sample_name,target_count,selected_count,strategy,status,rule_version,validator_version,created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            (sample_id, batch["batch_id"], batch["source_snapshot_id"], sample_name, target_count, len(selected), strategy, "open", batch["rule_version"], batch["validator_version"], now),
        )
        connection.executemany(
            "INSERT INTO review_sample_item(sample_id,candidate_id,ordinal,stratum,selected_at) VALUES (?,?,?,?,?)",
            [(sample_id, row["candidate_id"], ordinal, stratum, now) for ordinal, (row, stratum) in enumerate(selected, start=1)],
        )
        connection.execute(
            "INSERT INTO audit_event(entity_type,entity_id,event_type,actor,payload_json,event_at) VALUES (?,?,?,?,?,?)",
            ("review_sample", sample_id, "review_sample_created", "semantic-api", json.dumps({"target_count": target_count, "selected_count": len(selected), "strategy": strategy}, ensure_ascii=False), now),
        )
        connection.commit()
    except sqlite3.IntegrityError:
        if connection.in_transaction:
            connection.rollback()
        existing = connection.execute(
            "SELECT * FROM review_sample WHERE batch_id=? AND sample_name=?",
            (batch["batch_id"], sample_name),
        ).fetchone()
        if existing is None:
            raise
        return existing
    return connection.execute("SELECT * FROM review_sample WHERE sample_id=?", (sample_id,)).fetchone()


def review_sample_summary(connection: sqlite3.Connection, sample: sqlite3.Row) -> dict[str, Any]:
    counts = connection.execute(
        """
        SELECT c.review_state, count(*) AS count
        FROM review_sample_item i
        JOIN semantic_candidate c ON c.candidate_id=i.candidate_id
        WHERE i.sample_id=?
        GROUP BY c.review_state
        """,
        (sample["sample_id"],),
    ).fetchall()
    summary = {"pending": 0, "approved": 0, "modified": 0, "rejected": 0, "deferred": 0}
    for row in counts:
        summary[row["review_state"]] = int(row["count"])
    strata = connection.execute(
        "SELECT stratum, count(*) AS count FROM review_sample_item WHERE sample_id=? GROUP BY stratum ORDER BY stratum",
        (sample["sample_id"],),
    ).fetchall()
    return {
        "sampleId": sample["sample_id"],
        "sampleName": sample["sample_name"],
        "batchId": sample["batch_id"],
        "sourceSnapshotId": sample["source_snapshot_id"],
        "targetCount": int(sample["target_count"]),
        "selectedCount": int(sample["selected_count"]),
        "status": "completed" if summary["pending"] == 0 and sample["selected_count"] else sample["status"],
        "strategy": sample["strategy"],
        "ruleVersion": sample["rule_version"],
        "validatorVersion": sample["validator_version"],
        "pendingCount": summary["pending"],
        "approvedCount": summary["approved"],
        "modifiedCount": summary["modified"],
        "rejectedCount": summary["rejected"],
        "deferredCount": summary["deferred"],
        "strata": [{"stratum": row["stratum"], "count": int(row["count"])} for row in strata],
    }
