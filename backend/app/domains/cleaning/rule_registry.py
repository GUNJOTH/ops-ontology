"""Cleaning rule registry services."""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from fastapi import Depends, HTTPException

from app.core.auth import require_decision_auth
from app.core.db import sqlite_connection
from app.core.utils import utc_now
from app.domains.candidates.service import cleaning_registry, sync_cleaning_runs
from app.schemas.cleaning import CleaningRuleRegistrationRequest


def cleaning_rules() -> dict[str, Any]:
    sqlite = sqlite_connection()
    try:
        registry = cleaning_registry(sqlite)
        sync_cleaning_runs(sqlite, {row["replay_id"]: dict(row) for row in registry})
        sqlite.commit()
        runs = sqlite.execute(
            """
            SELECT r.rule_key,r.cleaning_type,r.rule_label,r.action_label,r.is_cleaning,r.replay_id,r.rule_version,r.enabled,
              u.cleaning_run_id,u.status,u.stage,u.candidate_count,u.pending_count,u.approved_count,u.published_count,
              u.preview_id,u.preview_sha256,u.preview_path,u.sample_path,u.approval_idempotency_key,u.publication_run_id,u.backup_path,
               u.source_write,u.formal_publication,u.archived,u.archived_at,u.archive_reason,u.last_error,u.updated_at
             FROM cleaning_rule_registry r JOIN cleaning_run u ON u.rule_key=r.rule_key AND u.replay_id=r.replay_id
             WHERE r.enabled=1 AND COALESCE(u.archived,0)=0 ORDER BY r.is_cleaning DESC,r.rule_key
            """
        ).fetchall()
        return {"rules": [dict(row) for row in runs]}
    finally:
        sqlite.close()

def register_cleaning_rule(request: CleaningRuleRegistrationRequest, actor: str = Depends(require_decision_auth)) -> dict[str, Any]:
    sqlite = sqlite_connection()
    try:
        now = utc_now()
        sqlite.execute(
            "INSERT INTO cleaning_rule_registry(rule_key,cleaning_type,rule_label,action_label,is_cleaning,replay_id,rule_version,enabled,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (request.rule_key, request.cleaning_type, request.rule_label, request.action_label, int(request.is_cleaning), request.replay_id, request.rule_version, 0, now, now),
        )
        sqlite.execute(
            "INSERT INTO cleaning_run(cleaning_run_id,rule_key,replay_id,status,created_at,updated_at) VALUES (?,?,?,?,?,?)",
            (f"cleaning-run-{request.replay_id}", request.rule_key, request.replay_id, "draft", now, now),
        )
        sqlite.execute(
            "INSERT INTO audit_event(entity_type,entity_id,event_type,actor,payload_json,event_at) VALUES (?,?,?,?,?,?)",
            ("cleaning_rule", request.rule_key, "cleaning_rule_registered", actor, json.dumps(request.model_dump(by_alias=True), ensure_ascii=False), now),
        )
        sqlite.commit()
        return {"ruleKey": request.rule_key, "replayId": request.replay_id, "status": "registered"}
    except sqlite3.IntegrityError as exc:
        sqlite.rollback()
        raise HTTPException(status_code=409, detail=f"清洗规则已存在或 replay_id 冲突：{exc}") from exc
    finally:
        sqlite.close()
