"""Read/query semantic workflow data and record auditable review decisions."""
from __future__ import annotations

import json
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

BACKEND_ROOT = Path(__file__).resolve().parents[1]
DEPENDENCY_DIR = BACKEND_ROOT / ".deps"
if DEPENDENCY_DIR.exists():
    sys.path.insert(0, str(DEPENDENCY_DIR))

import duckdb  # type: ignore
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field

PROJECT_ROOT = BACKEND_ROOT.parent
SYSTEM_ROOT = PROJECT_ROOT / "system"
DATA_DIR = SYSTEM_ROOT / "data"
SQLITE_DB = DATA_DIR / "semantic_workflow.sqlite3"
DUCKDB_DB = DATA_DIR / "semantic_analytics_v155.duckdb"

app = FastAPI(title="设备语义治理 API", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sqlite_connection() -> sqlite3.Connection:
    if not SQLITE_DB.exists():
        raise HTTPException(status_code=503, detail="SQLite 工作流数据库不存在")
    connection = sqlite3.connect(str(SQLITE_DB), timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=30000")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def duckdb_connection() -> Any:
    if not DUCKDB_DB.exists():
        raise HTTPException(status_code=503, detail="DuckDB 分析数据库不存在")
    return duckdb.connect(str(DUCKDB_DB), read_only=True)


def latest_batch(connection: sqlite3.Connection) -> sqlite3.Row:
    row = connection.execute("SELECT * FROM batch_run ORDER BY started_at DESC LIMIT 1").fetchone()
    if row is None:
        raise HTTPException(status_code=503, detail="SQLite 中没有可用批次")
    return row


def parse_json_array(value: str | None) -> list[str]:
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return []
    return [str(item) for item in parsed] if isinstance(parsed, list) else []


def row_to_candidate(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "candidateId": row["candidate_id"],
        "batchId": row["batch_id"],
        "siteId": row["site_id"],
        "assetNumber": row["asset_number"],
        "originalDescription": row["original_description"],
        "candidateDescription": row["candidate_description"],
        "kks": row["location_code"] or "",
        "locationDescription": row["location_description"] or "",
        "locationParent": row["location_parent"] or "",
        "classificationDescription": row["classification_description"] or "",
        "confidence": row["confidence"],
        "validatorStatus": row["validator_status"],
        "reviewState": row["review_state"],
        "reasonCodes": parse_json_array(row["reason_codes_json"]),
        "evidenceLevel": row["evidence_level"],
        "updatedAt": row["created_at"],
    }


class ReviewRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    candidate_id: str = Field(alias="candidateId", min_length=1)
    decision: Literal["approved", "modified", "rejected", "deferred"]
    reviewed_description: str | None = Field(default=None, alias="reviewedDescription")
    note: str | None = None
    idempotency_key: str = Field(alias="idempotencyKey", min_length=1, max_length=200)


class ReviewResponse(BaseModel):
    review_id: str = Field(alias="reviewId")
    candidate_id: str = Field(alias="candidateId")
    decision: str
    review_state: str = Field(alias="reviewState")
    approval_receipt: str = Field(alias="approvalReceipt")
    reviewed_at: str = Field(alias="reviewedAt")


@app.get("/api/health")
def health() -> dict[str, Any]:
    sqlite_ok = SQLITE_DB.exists()
    duckdb_ok = DUCKDB_DB.exists()
    return {"status": "ok" if sqlite_ok and duckdb_ok else "degraded", "sqlite": sqlite_ok, "duckdb": duckdb_ok, "sourceWrite": False}


@app.get("/api/dashboard")
def dashboard() -> dict[str, Any]:
    sqlite = sqlite_connection()
    try:
        batch = latest_batch(sqlite)
        source_snapshot_id = batch["source_snapshot_id"]
        batch_id = batch["batch_id"]
        counts = sqlite.execute(
            """
            SELECT
              count(*) AS total,
              sum(CASE WHEN validator_status = 'candidate' THEN 1 ELSE 0 END) AS candidate_count,
              sum(CASE WHEN review_state = 'pending' THEN 1 ELSE 0 END) AS pending_count,
              sum(CASE WHEN review_state IN ('approved','modified') THEN 1 ELSE 0 END) AS approved_count,
              sum(CASE WHEN review_state = 'rejected' THEN 1 ELSE 0 END) AS rejected_count,
              sum(CASE WHEN validator_status = 'blocked' THEN 1 ELSE 0 END) AS blocked_count
            FROM semantic_candidate WHERE batch_id=?
            """,
            (batch_id,),
        ).fetchone()
        sample_count = sqlite.execute(
            "SELECT count(*) FROM semantic_candidate WHERE batch_id=? AND review_state='pending' AND validator_status='candidate'",
            (batch_id,),
        ).fetchone()[0]
    finally:
        sqlite.close()

    duck = duckdb_connection()
    try:
        sites = duck.execute(
            """
            SELECT SITEID, count(*) AS device_count,
              round(count(*) * 100.0 / sum(count(*)) OVER (), 1) AS share
            FROM semantic_candidate_fact
            GROUP BY SITEID ORDER BY device_count DESC LIMIT 10
            """
        ).fetchall()
        coverage = duck.execute("SELECT * FROM v_context_coverage LIMIT 1").fetchone()
        coverage_columns = [item[0] for item in duck.description] if coverage is not None else []
    finally:
        duck.close()

    coverage_map = dict(zip(coverage_columns, coverage or []))
    row_count = int(coverage_map.get("row_count") or counts["total"] or 0)

    def percentage(key: str) -> float:
        return round((int(coverage_map.get(key) or 0) * 100.0 / row_count), 1) if row_count else 0

    return {
        "batchId": batch_id,
        "ruleVersion": batch["rule_version"],
        "sourceSnapshot": f"HD_SAAS / {source_snapshot_id[:12]}",
        "inputCount": int(batch["input_count"]),
        "candidateCount": int(counts["candidate_count"] or 0),
        "pendingReviewCount": int(counts["pending_count"] or 0),
        "approvedCount": int(counts["approved_count"] or 0),
        "publishedCount": int(batch["published_count"] or 0),
        "blockedCount": int(counts["blocked_count"] or 0),
        "readOnlySource": True,
        "samplesReady": min(300, int(sample_count)),
        "sites": [{"siteId": row[0], "count": int(row[1]), "share": float(row[2])} for row in sites],
        "contextCoverage": [
            {"label": "KKS / 位置", "value": percentage("location_code_rows")},
            {"label": "位置父级", "value": percentage("location_parent_rows")},
            {"label": "分类", "value": percentage("classification_rows")},
            {"label": "规格 / 特征", "value": percentage("specification_rows")},
        ],
    }


@app.get("/api/candidates")
def candidates(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    search: str | None = None,
    site_id: str | None = None,
    classification: str | None = None,
    quick_filter: Literal["all", "pending", "context", "low"] = "all",
) -> dict[str, Any]:
    sqlite = sqlite_connection()
    try:
        batch = latest_batch(sqlite)
        where = ["c.batch_id = ?"]
        parameters: list[Any] = [batch["batch_id"]]
        if search and search.strip():
            value = f"%{search.strip()}%"
            where.append("(d.asset_number LIKE ? OR d.original_description LIKE ? OR c.candidate_description LIKE ? OR d.location_code LIKE ? OR d.location_description LIKE ?)")
            parameters.extend([value] * 5)
        if site_id:
            where.append("d.site_id = ?")
            parameters.append(site_id)
        if classification:
            where.append("d.classification_description = ?")
            parameters.append(classification)
        if quick_filter == "pending":
            where.append("c.review_state = 'pending'")
        elif quick_filter == "context":
            where.append("(c.reason_codes_json LIKE '%LOCATION%' OR c.reason_codes_json LIKE '%CONTEXT%')")
        elif quick_filter == "low":
            where.append("c.confidence = 'low'")
        where_sql = " AND ".join(where)
        total = sqlite.execute(
            f"SELECT count(*) FROM semantic_candidate c JOIN device_identity d ON d.device_id=c.device_id WHERE {where_sql}",
            parameters,
        ).fetchone()[0]
        parameters.extend([page_size, (page - 1) * page_size])
        rows = sqlite.execute(
            f"""
            SELECT c.candidate_id,c.batch_id,d.site_id,d.asset_number,c.original_description,
              c.candidate_description,d.location_code,d.location_description,d.location_parent,
              d.classification_description,c.confidence,c.validator_status,c.review_state,
              c.reason_codes_json,c.evidence_level,c.created_at
            FROM semantic_candidate c JOIN device_identity d ON d.device_id=c.device_id
            WHERE {where_sql}
            ORDER BY d.site_id, d.asset_number, c.candidate_id
            LIMIT ? OFFSET ?
            """,
            parameters,
        ).fetchall()
        return {"rows": [row_to_candidate(row) for row in rows], "total": int(total), "page": page, "pageSize": page_size}
    finally:
        sqlite.close()


@app.get("/api/candidates/{candidate_id}")
def candidate_detail(candidate_id: str) -> dict[str, Any]:
    sqlite = sqlite_connection()
    try:
        row = sqlite.execute(
            """
            SELECT c.candidate_id,c.batch_id,c.original_description,c.candidate_description,c.confidence,
              c.validator_status,c.review_state,c.reason_codes_json,c.evidence_level,c.applied_rule_ids_json,
              c.rule_version,c.validator_version,c.created_at,d.source_asset_id,d.site_id,d.asset_number,
              d.source_row_hash,d.context_hash,d.location_code,d.location_description,d.location_parent,
              d.classification_description
            FROM semantic_candidate c JOIN device_identity d ON d.device_id=c.device_id
            WHERE c.candidate_id=?
            """,
            (candidate_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="候选记录不存在")
        result = row_to_candidate(row)
        result.update({
            "assetId": row["source_asset_id"] or "",
            "sourceRowHash": row["source_row_hash"],
            "contextHash": row["context_hash"],
            "specificationCount": 0,
            "featureCount": 0,
            "parentChildEvidence": "当前 SQLite 流程库只保存摘要；详细上下文从 DuckDB 分析库读取。",
            "appliedRules": parse_json_array(row["applied_rule_ids_json"]),
            "validatorVersion": row["validator_version"],
            "ruleVersion": row["rule_version"],
        })
    finally:
        sqlite.close()

    duck = duckdb_connection()
    try:
        context = duck.execute(
            """
            SELECT SPEC_COUNT, FEATURE_COUNT, PARENT_ASSET_COUNT, RELATION_COUNT,
              CONTEXT_EVIDENCE_LEVEL, SEMANTIC_REASON_CODES, SEMANTIC_EVIDENCE_JSON,
              LOCATION_DESCRIPTION, LOCATION_PARENT, CLASSIFICATION_DESCRIPTION,
              CONTEXT_JSON
            FROM semantic_candidate_fact WHERE CANDIDATE_ID=? LIMIT 1
            """,
            (candidate_id,),
        ).fetchone()
        columns = [item[0] for item in duck.description] if context is not None else []
    finally:
        duck.close()
    if context is not None:
        values = dict(zip(columns, context))
        result["specificationCount"] = int(values.get("SPEC_COUNT") or 0)
        result["featureCount"] = int(values.get("FEATURE_COUNT") or 0)
        result["evidenceLevel"] = values.get("CONTEXT_EVIDENCE_LEVEL") or result["evidenceLevel"]
        result["parentChildEvidence"] = f"父资产 {values.get('PARENT_ASSET_COUNT') or 0} 条，关系 {values.get('RELATION_COUNT') or 0} 条"
        if values.get("SEMANTIC_EVIDENCE_JSON"):
            result["evidenceJson"] = values["SEMANTIC_EVIDENCE_JSON"]
    return result


@app.post("/api/reviews", response_model=ReviewResponse, response_model_by_alias=True)
def create_review(request: ReviewRequest) -> dict[str, Any]:
    description = (request.reviewed_description or "").strip()
    note = (request.note or "").strip()
    if request.decision in {"approved", "modified"} and not description:
        raise HTTPException(status_code=422, detail="通过或修改后通过必须填写最终统一描述")
    if request.decision in {"modified", "rejected", "deferred"} and not note:
        raise HTTPException(status_code=422, detail="该审核动作必须填写说明")

    sqlite = sqlite_connection()
    try:
        existing_events = sqlite.execute(
            "SELECT entity_id,payload_json FROM audit_event WHERE event_type='review_submitted' ORDER BY event_id DESC LIMIT 1000"
        ).fetchall()
        for event in existing_events:
            try:
                payload = json.loads(event["payload_json"])
            except json.JSONDecodeError:
                continue
            if payload.get("idempotency_key") == request.idempotency_key:
                existing = sqlite.execute(
                    "SELECT review_id,candidate_id,decision,reviewed_at,approval_receipt FROM review_decision WHERE review_id=?",
                    (event["entity_id"],),
                ).fetchone()
                if existing:
                    return {"reviewId": existing["review_id"], "candidateId": existing["candidate_id"], "decision": existing["decision"], "reviewState": existing["decision"], "approvalReceipt": existing["approval_receipt"], "reviewedAt": existing["reviewed_at"]}

        candidate = sqlite.execute(
            "SELECT candidate_id, batch_id, validator_status, review_state, candidate_description FROM semantic_candidate WHERE candidate_id=?",
            (request.candidate_id,),
        ).fetchone()
        if candidate is None:
            raise HTTPException(status_code=404, detail="候选记录不存在")
        if candidate["review_state"] != "pending":
            raise HTTPException(status_code=409, detail="该候选已经完成审核，不能重复提交")
        if request.decision in {"approved", "modified"} and candidate["validator_status"] != "candidate":
            raise HTTPException(status_code=409, detail="只有通过硬校验的 candidate 才能批准或修改后通过")

        now = utc_now()
        review_id = f"review-{uuid.uuid4().hex}"
        approval_receipt = f"receipt-{uuid.uuid4().hex}"
        review_state = request.decision
        sqlite.execute("BEGIN IMMEDIATE")
        sqlite.execute(
            """
            INSERT INTO review_decision(review_id,candidate_id,decision,reviewed_description,reason_code,review_note,reviewer,approval_receipt,reviewed_at)
            VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (review_id, request.candidate_id, request.decision, description or None, f"REVIEW_{request.decision.upper()}", note or None, "local-user", approval_receipt, now),
        )
        sqlite.execute("UPDATE semantic_candidate SET review_state=? WHERE candidate_id=?", (review_state, request.candidate_id))
        batch_id = candidate["batch_id"]
        sqlite.execute(
            """
            UPDATE batch_run SET
              needs_review_count=(SELECT count(*) FROM semantic_candidate WHERE batch_id=? AND review_state='pending'),
              approved_count=(SELECT count(*) FROM semantic_candidate WHERE batch_id=? AND review_state IN ('approved','modified'))
            WHERE batch_id=?
            """,
            (batch_id, batch_id, batch_id),
        )
        sqlite.execute(
            "INSERT INTO audit_event(entity_type,entity_id,event_type,actor,payload_json,event_at) VALUES (?,?,?,?,?,?)",
            ("review", review_id, "review_submitted", "local-user", json.dumps({"candidate_id": request.candidate_id, "decision": request.decision, "idempotency_key": request.idempotency_key}, ensure_ascii=False), now),
        )
        sqlite.commit()
        return {"reviewId": review_id, "candidateId": request.candidate_id, "decision": request.decision, "reviewState": review_state, "approvalReceipt": approval_receipt, "reviewedAt": now}
    except HTTPException:
        if sqlite.in_transaction:
            sqlite.rollback()
        raise
    except sqlite3.IntegrityError as exc:
        if sqlite.in_transaction:
            sqlite.rollback()
        raise HTTPException(status_code=409, detail=f"审核写入冲突：{exc}") from exc
    finally:
        sqlite.close()
