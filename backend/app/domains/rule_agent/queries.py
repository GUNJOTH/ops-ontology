"""Rule agent and semantic reasoning routes (native APIRouter)."""
from __future__ import annotations

from typing import Any, Literal

from fastapi import Query

from app.core.config import (
    RULE_AGENT_API_KEY,
    RULE_AGENT_BASE_URL,
    RULE_AGENT_MAX_ATTEMPTS,
    RULE_AGENT_MODEL,
    RULE_AGENT_TIMEOUT,
)
from app.core.db import sqlite_connection

from .service import (
    rule_agent_profile,
    rule_agent_proposal_payload,
)


def rule_agent_profile_endpoint(sample_size: int = Query(default=120, ge=20, le=500)) -> dict[str, Any]:
    sqlite = sqlite_connection()
    try:
        return {"profile": rule_agent_profile(sqlite, sample_size), "configured": bool(RULE_AGENT_API_KEY and RULE_AGENT_BASE_URL), "sourceWrite": False}
    finally:
        sqlite.close()
def rule_agent_status() -> dict[str, Any]:
    """Return runtime configuration and recent failure diagnostics without a profile scan."""
    connection = sqlite_connection()
    try:
        latest = connection.execute(
            "SELECT run_id,status,error_code,retryable,attempt_count,error_message,created_at,finished_at "
            "FROM rule_agent_run ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        failed_count = int(connection.execute("SELECT count(*) FROM rule_agent_run WHERE status='failed'").fetchone()[0])
        return {
            "configured": bool(RULE_AGENT_API_KEY and RULE_AGENT_BASE_URL and RULE_AGENT_MODEL),
            "model": RULE_AGENT_MODEL,
            "baseUrlConfigured": bool(RULE_AGENT_BASE_URL),
            "apiKeyConfigured": bool(RULE_AGENT_API_KEY),
            "timeoutSeconds": RULE_AGENT_TIMEOUT,
            "maxAttempts": RULE_AGENT_MAX_ATTEMPTS,
            "failedRunCount": failed_count,
            "latestRun": dict(latest) if latest else None,
            "fallbackPolicy": "仅在模型返回但未绑定本地规则时使用确定性目录；模型调用失败不冒充 AI 结果",
            "sourceWrite": False,
            "formalPublication": False,
        }
    finally:
        connection.close()



def rule_agent_proposals(status: Literal["all", "draft", "previewed", "replayed", "confirmed", "enabled", "rejected", "failed"] = "all") -> dict[str, Any]:
    sqlite = sqlite_connection()
    try:
        where = "WHERE discovery_filter_status='eligible'"
        params: tuple[Any, ...] = ()
        if status != "all":
            where += " AND status=?"
            params = (status,)
        rows = sqlite.execute(f"SELECT * FROM rule_agent_proposal {where} ORDER BY updated_at DESC,proposal_id", params).fetchall()
        filtered_count = int(sqlite.execute("SELECT count(*) FROM rule_agent_proposal WHERE discovery_filter_status='filtered'").fetchone()[0])
        runs = sqlite.execute("SELECT run_id,batch_id,source_snapshot_id,eligible_count,sampled_count,model,provider_base_url,status,created_at,finished_at,error_message FROM rule_agent_run ORDER BY created_at DESC LIMIT 20").fetchall()
        return {"proposals": [rule_agent_proposal_payload(row) for row in rows], "filteredProposalCount": filtered_count, "runs": [dict(row) for row in runs], "sourceWrite": False}
    finally:
        sqlite.close()
