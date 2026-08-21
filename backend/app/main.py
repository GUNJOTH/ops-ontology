"""Read/query semantic workflow data and record auditable review decisions."""
from __future__ import annotations

import json
import csv
import hashlib
import io
import importlib.util
import os
import re
import sqlite3
import sys
import time
import unicodedata
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from app.core.config import (
    AI_REVIEW_MANIFEST,
    AI_REVIEW_SAMPLE_CSV,
    BACKEND_ROOT,
    BACKUP_DIR,
    CANONICAL_SEMANTICS_DB,
    CLEANING_TASK_DIR,
    DATA_DIR,
    DEPENDENCY_DIR,
    DUCKDB_DB,
    IDENTITY_RESULT_ROOT,
    METADATA_DUCKDB_DB,
    METADATA_RESULT_DIR,
    METADATA_SQLITE_DB,
    PROJECT_ROOT,
    RULE_AGENT_API_KEY,
    RULE_AGENT_BASE_URL,
    RULE_AGENT_CATALOG_VERSION,
    RULE_AGENT_DIR,
    RULE_AGENT_MAX_ATTEMPTS,
    RULE_AGENT_MAX_PROPOSALS,
    RULE_AGENT_MAX_TOKENS,
    RULE_AGENT_MODEL,
    RULE_AGENT_RETRY_BACKOFF,
    RULE_AGENT_THINKING,
    RULE_AGENT_TIMEOUT,
    SEMANTIC_CONTEXT_FILE,
    SQLITE_DB,
    SYSTEM_ROOT,
    UNIFIED_SEMANTICS_DB,
)

if DEPENDENCY_DIR.exists():
    sys.path.insert(0, str(DEPENDENCY_DIR))

from app.core.utils import decode_json_value, ontology_trace_id, parse_json_array, utc_now
from app.core.sparql_guard import guard_text
from app.core.audit import append_audit_event
from app.core.canonical_reader import CanonicalReader, canonical_run_row
from app.core.metrics import runtime_metrics
from app.core.semantic_release import (
    activate_release,
    approve_release,
    backup_release,
    load_active_release,
    load_registry as load_semantic_release_registry,
    _load_release as load_semantic_release,
    rollback_release,
)
from app.core.idempotency import find_audit_event_by_idempotency
from app.core.tx import begin_write, commit_write
from app.core.db import (
    canonical_semantics_connection as _core_canonical_semantics_connection,
    configure_workflow_schema,
    duckdb_connection as _core_duckdb_connection,
    identity_result_connection as _core_identity_result_connection,
    latest_identity_result_db as _core_latest_identity_result_db,
    metadata_duckdb_connection as _core_metadata_duckdb_connection,
    metadata_sqlite_connection as _core_metadata_sqlite_connection,
    raw_workflow_connection as _core_raw_workflow_connection,
    sqlite_connection as _core_sqlite_connection,
    unified_semantics_connection as _core_unified_semantics_connection,
    unified_semantics_write_connection as _core_unified_semantics_write_connection,
)

import duckdb  # type: ignore
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field
from openai import OpenAI

from app.agent.client import (
    RuleAgentCallError,
    invoke_rule_agent_completion,
    parse_rule_agent_json,
    rule_agent_failure_detail,
    rule_agent_failure_fields,
)

_SEMANTIC_CONTEXT_CLUSTER_CACHE: dict[tuple[str, int, int], list[dict[str, Any]]] = {}


class RuleAgentCallError(RuntimeError):
    """A model-call failure with a stable code for audit and retry policy."""

    def __init__(self, code: str, message: str, retryable: bool, attempts: int) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.attempts = attempts


def invoke_rule_agent_completion(messages: list[dict[str, str]], task_label: str) -> str:
    """Call the OpenAI-compatible gateway with bounded, classified retries."""
    if not RULE_AGENT_API_KEY or not RULE_AGENT_BASE_URL or not RULE_AGENT_MODEL:
        raise RuleAgentCallError("agent_not_configured", f"{task_label}尚未配置模型环境", False, 0)
    base_url = RULE_AGENT_BASE_URL.rstrip("/")
    if not base_url.endswith("/v1"):
        base_url += "/v1"
    last_error: RuleAgentCallError | None = None
    for attempt in range(1, RULE_AGENT_MAX_ATTEMPTS + 1):
        try:
            client = OpenAI(
                base_url=base_url,
                api_key=RULE_AGENT_API_KEY,
                timeout=RULE_AGENT_TIMEOUT,
                max_retries=0,
            )
            completion = client.chat.completions.create(
                model=RULE_AGENT_MODEL,
                max_tokens=RULE_AGENT_MAX_TOKENS,
                temperature=0,
                extra_body={"thinking": {"type": RULE_AGENT_THINKING}},
                messages=messages,
            )
            text = completion.choices[0].message.content if completion.choices else ""
            if isinstance(text, list):
                text = "".join(item.get("text", "") for item in text if isinstance(item, dict))
            text = str(text or "").strip()
            if text:
                return text
            last_error = RuleAgentCallError("empty_response", f"{task_label}未返回内容", True, attempt)
        except APIStatusError as exc:
            status = int(getattr(exc, "status_code", 0) or 0)
            retryable = status in {408, 409, 425, 429} or status >= 500
            last_error = RuleAgentCallError(
                f"provider_http_{status or 'unknown'}",
                f"{task_label}调用失败：HTTP {status or 'unknown'}",
                retryable,
                attempt,
            )
        except (APIConnectionError, APITimeoutError, TimeoutError) as exc:
            last_error = RuleAgentCallError(
                f"provider_{type(exc).__name__.lower()}",
                f"{task_label}调用失败：{type(exc).__name__}",
                True,
                attempt,
            )
        except Exception as exc:
            last_error = RuleAgentCallError(
                f"provider_{type(exc).__name__.lower()}",
                f"{task_label}调用失败：{type(exc).__name__}",
                False,
                attempt,
            )
        if last_error is not None and (not last_error.retryable or attempt >= RULE_AGENT_MAX_ATTEMPTS):
            raise last_error
        time.sleep(RULE_AGENT_RETRY_BACKOFF * attempt)
    raise last_error or RuleAgentCallError("unknown_agent_error", f"{task_label}调用失败", False, RULE_AGENT_MAX_ATTEMPTS)


def parse_rule_agent_json(text: str, task_label: str) -> Any:
    """Parse JSON-only model output, allowing one harmless Markdown wrapper."""
    cleaned = re.sub(r"^```(?:json)?\s*", "", text.strip(), flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned, flags=re.IGNORECASE)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start < 0 or end <= start:
            raise RuleAgentCallError("invalid_json", f"{task_label}返回的不是有效 JSON", True, 1)
        try:
            return json.loads(cleaned[start : end + 1])
        except json.JSONDecodeError as exc:
            raise RuleAgentCallError("invalid_json", f"{task_label}返回的不是有效 JSON", True, 1) from exc


def rule_agent_failure_detail(exc: RuleAgentCallError) -> str:
    return json.dumps(
        {
            "code": exc.code,
            "message": exc.message,
            "retryable": exc.retryable,
            "attempts": exc.attempts,
        },
        ensure_ascii=False,
    )


def rule_agent_failure_fields(detail: object) -> tuple[str, bool, int]:
    """Read the stable error envelope without exposing request credentials."""
    try:
        payload = json.loads(str(detail))
    except (TypeError, json.JSONDecodeError):
        return "unknown_agent_error", False, 0
    if not isinstance(payload, dict):
        return "unknown_agent_error", False, 0
    return (
        str(payload.get("code") or "unknown_agent_error"),
        bool(payload.get("retryable")),
        max(0, int(payload.get("attempts") or 0)),
    )


# Stage 2 runtime binding. The old declarations above are retained only as a
# short-lived import bridge; all route code below calls the extracted client.
from app.agent.client import (
    RuleAgentCallError as _AgentRuleAgentCallError,
    invoke_rule_agent_completion as _agent_invoke_rule_agent_completion,
    parse_rule_agent_json as _agent_parse_rule_agent_json,
    rule_agent_failure_detail as _agent_rule_agent_failure_detail,
    rule_agent_failure_fields as _agent_rule_agent_failure_fields,
)

RuleAgentCallError = _AgentRuleAgentCallError
invoke_rule_agent_completion = _agent_invoke_rule_agent_completion
parse_rule_agent_json = _agent_parse_rule_agent_json
rule_agent_failure_detail = _agent_rule_agent_failure_detail
rule_agent_failure_fields = _agent_rule_agent_failure_fields

app = FastAPI(title="设备语义治理 API", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


@app.middleware("http")
async def observe_runtime_request(request: Request, call_next: Any):
    started = time.perf_counter()
    status_code = 500
    try:
        response = await call_next(request)
        status_code = response.status_code
        return response
    finally:
        route = request.scope.get("route")
        path = getattr(route, "path", request.url.path)
        runtime_metrics.inc(
            "semantic_http_requests_total",
            labels={"method": request.method, "path": path, "status": status_code},
        )
        runtime_metrics.observe(
            "semantic_http_request_duration_seconds",
            time.perf_counter() - started,
            labels={"method": request.method, "path": path},
        )


# --- Optional write guard and acting-reviewer resolution (F10) ---
# The API is read-only by default and all human-decision writes are local-only.
# When SEMANTIC_API_TOKEN is set, the decision/review/approval write endpoints
# below additionally require "Authorization: Bearer <token>" (401 otherwise).
# When it is unset, local no-token behaviour is unchanged.  The acting reviewer
# recorded in those writes is resolved from the X-Reviewer header when present,
# then the SEMANTIC_REVIEWER environment variable, then "local-user".
_SEMANTIC_API_TOKEN = os.environ.get("SEMANTIC_API_TOKEN", "").strip()
_SEMANTIC_REVIEWER = os.environ.get("SEMANTIC_REVIEWER", "").strip()


def resolve_reviewer(x_reviewer: str | None) -> str:
    """Resolve the acting reviewer: X-Reviewer header > env > local-user."""
    if x_reviewer and x_reviewer.strip():
        return x_reviewer.strip()
    if _SEMANTIC_REVIEWER:
        return _SEMANTIC_REVIEWER
    return "local-user"


def require_decision_auth(
    authorization: str | None = Header(default=None, alias="Authorization"),
    x_reviewer: str | None = Header(default=None, alias="X-Reviewer"),
) -> str:
    """Enforce the optional write token and return the acting reviewer.

    This dependency is the only authentication mechanism for the local write
    endpoints.  It intentionally does nothing when SEMANTIC_API_TOKEN is unset
    so existing local deployments keep working exactly as before.
    """
    if _SEMANTIC_API_TOKEN:
        expected = f"Bearer {_SEMANTIC_API_TOKEN}"
        if authorization != expected:
            raise HTTPException(status_code=401, detail="缺少或无效的 API Token")
    return resolve_reviewer(x_reviewer)


_DOMAIN_ROUTE_SPECS: list[tuple[str, list[str], Any, dict[str, Any]]] = []


def _domain_route(path: str, methods: list[str], **options: Any):
    def decorator(endpoint: Any) -> Any:
        _DOMAIN_ROUTE_SPECS.append((path, methods, endpoint, options))
        return endpoint

    return decorator


def domain_get(path: str, **options: Any):
    return _domain_route(path, ["GET"], **options)


def domain_post(path: str, **options: Any):
    return _domain_route(path, ["POST"], **options)


def sqlite_connection() -> sqlite3.Connection:
    return _core_sqlite_connection()


def latest_identity_result_db() -> Path:
    return _core_latest_identity_result_db()


def identity_result_connection() -> sqlite3.Connection:
    return _core_identity_result_connection()


def unified_semantics_connection() -> sqlite3.Connection:
    return _core_unified_semantics_connection()


def unified_semantics_write_connection() -> sqlite3.Connection:
    """Open only the local relational overlay for audit-ledger writes."""
    return _core_unified_semantics_write_connection()


def canonical_semantics_connection() -> sqlite3.Connection:
    """Read the canonical RDF projection; API routes never write this DB."""
    return _core_canonical_semantics_connection()


def ensure_review_sample_schema(connection: sqlite3.Connection) -> None:
    """Apply the small local workflow migration without touching source data."""
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS review_sample (
          sample_id TEXT PRIMARY KEY,
          batch_id TEXT NOT NULL REFERENCES batch_run(batch_id),
          source_snapshot_id TEXT NOT NULL REFERENCES source_snapshot(source_snapshot_id),
          sample_name TEXT NOT NULL,
          target_count INTEGER NOT NULL,
          selected_count INTEGER NOT NULL DEFAULT 0,
          strategy TEXT NOT NULL,
          status TEXT NOT NULL CHECK (status IN ('open','completed','cancelled')),
          rule_version TEXT NOT NULL,
          validator_version TEXT NOT NULL,
          created_at TEXT NOT NULL,
          UNIQUE (batch_id, sample_name)
        );
        CREATE TABLE IF NOT EXISTS review_sample_item (
          sample_id TEXT NOT NULL REFERENCES review_sample(sample_id),
          candidate_id TEXT NOT NULL REFERENCES semantic_candidate(candidate_id),
          ordinal INTEGER NOT NULL,
          stratum TEXT NOT NULL,
          selected_at TEXT NOT NULL,
          PRIMARY KEY (sample_id, candidate_id),
          UNIQUE (sample_id, ordinal)
        );
        CREATE INDEX IF NOT EXISTS ix_review_sample_batch ON review_sample(batch_id, status);
        CREATE INDEX IF NOT EXISTS ix_review_sample_item_candidate ON review_sample_item(candidate_id);
        CREATE TABLE IF NOT EXISTS ai_review_run (
          run_id TEXT PRIMARY KEY,
          idempotency_key TEXT NOT NULL UNIQUE,
          batch_id TEXT NOT NULL REFERENCES batch_run(batch_id),
          scope TEXT NOT NULL CHECK (scope IN ('sample','batch')),
          policy_version TEXT NOT NULL,
          eligible_count INTEGER NOT NULL DEFAULT 0,
          applied_count INTEGER NOT NULL DEFAULT 0,
          skipped_count INTEGER NOT NULL DEFAULT 0,
          status TEXT NOT NULL CHECK (status IN ('completed','failed')),
          started_at TEXT NOT NULL,
          finished_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_ai_review_run_batch ON ai_review_run(batch_id, finished_at);
        CREATE TABLE IF NOT EXISTS ai_review_decision (
          decision_id TEXT PRIMARY KEY,
          sample_id TEXT NOT NULL,
          candidate_id TEXT NOT NULL UNIQUE REFERENCES semantic_candidate(candidate_id),
          decision TEXT NOT NULL CHECK (decision IN ('keep_original','accept_candidate','needs_review')),
          note TEXT NOT NULL DEFAULT '',
          reviewer TEXT NOT NULL,
          idempotency_key TEXT NOT NULL UNIQUE,
          reviewed_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_ai_review_decision_sample ON ai_review_decision(sample_id, decision);
        CREATE TABLE IF NOT EXISTS ai_cluster_decision (
          cluster_id TEXT PRIMARY KEY,
          decision TEXT NOT NULL CHECK (decision IN ('keep_original','accept_candidate','needs_review')),
          member_count INTEGER NOT NULL,
          applied_count INTEGER NOT NULL DEFAULT 0,
          note TEXT NOT NULL DEFAULT '',
          reviewer TEXT NOT NULL,
          idempotency_key TEXT NOT NULL UNIQUE,
          reviewed_at TEXT NOT NULL,
          source_write INTEGER NOT NULL DEFAULT 0 CHECK (source_write = 0),
          formal_publication INTEGER NOT NULL DEFAULT 0 CHECK (formal_publication = 0)
        );
        CREATE INDEX IF NOT EXISTS ix_ai_cluster_decision_decision ON ai_cluster_decision(decision);
        CREATE TABLE IF NOT EXISTS formal_approval_queue (
          queue_id TEXT PRIMARY KEY,
          candidate_id TEXT NOT NULL UNIQUE REFERENCES semantic_candidate(candidate_id),
          cluster_id TEXT NOT NULL,
          replay_id TEXT NOT NULL REFERENCES replay_run(replay_id),
          proposed_decision TEXT NOT NULL CHECK (proposed_decision IN ('approved','modified','rejected','deferred')),
          proposed_description TEXT NOT NULL,
          status TEXT NOT NULL CHECK (status IN ('pending','approved','modified','rejected','deferred')),
          note TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          source_write INTEGER NOT NULL DEFAULT 0 CHECK (source_write = 0),
          formal_publication INTEGER NOT NULL DEFAULT 0 CHECK (formal_publication = 0)
        );
        CREATE INDEX IF NOT EXISTS ix_formal_approval_queue_status ON formal_approval_queue(status, created_at);
        CREATE INDEX IF NOT EXISTS ix_published_site_asset ON published_description(site_id, asset_number, publication_id);
        CREATE INDEX IF NOT EXISTS ix_published_candidate ON published_description(candidate_id);
        CREATE TABLE IF NOT EXISTS rule_agent_run (
          run_id TEXT PRIMARY KEY,
          idempotency_key TEXT NOT NULL UNIQUE,
          batch_id TEXT NOT NULL REFERENCES batch_run(batch_id),
          source_snapshot_id TEXT NOT NULL REFERENCES source_snapshot(source_snapshot_id),
          eligible_count INTEGER NOT NULL DEFAULT 0,
          sampled_count INTEGER NOT NULL DEFAULT 0,
          model TEXT NOT NULL,
          provider_base_url TEXT NOT NULL,
            profile_json TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL CHECK (status IN ('running','completed','failed')),
            error_message TEXT,
            error_code TEXT,
            retryable INTEGER NOT NULL DEFAULT 0 CHECK (retryable IN (0,1)),
            attempt_count INTEGER NOT NULL DEFAULT 0,
            fallback_used INTEGER NOT NULL DEFAULT 0 CHECK (fallback_used IN (0,1)),
            created_at TEXT NOT NULL,
            finished_at TEXT
        );
        CREATE INDEX IF NOT EXISTS ix_rule_agent_run_batch ON rule_agent_run(batch_id, created_at);
        CREATE TABLE IF NOT EXISTS rule_agent_proposal (
          proposal_id TEXT PRIMARY KEY,
          run_id TEXT NOT NULL REFERENCES rule_agent_run(run_id),
          rule_key TEXT NOT NULL,
          rule_version TEXT NOT NULL,
          title TEXT NOT NULL,
          objective TEXT NOT NULL,
          operation TEXT NOT NULL,
          condition_json TEXT NOT NULL DEFAULT '{}',
          parameters_json TEXT NOT NULL DEFAULT '{}',
          scope_json TEXT NOT NULL DEFAULT '{}',
          evidence_json TEXT NOT NULL DEFAULT '{}',
          examples_json TEXT NOT NULL DEFAULT '[]',
          expected_count INTEGER NOT NULL DEFAULT 0,
          confidence REAL NOT NULL DEFAULT 0,
          risk_level TEXT NOT NULL CHECK (risk_level IN ('low','medium','high')),
          status TEXT NOT NULL CHECK (status IN ('draft','previewed','replayed','confirmed','enabled','rejected','failed')),
          discovery_filter_status TEXT NOT NULL DEFAULT 'eligible' CHECK (discovery_filter_status IN ('eligible','filtered')),
          discovery_filter_reason TEXT,
          discovery_filtered_at TEXT,
          preview_path TEXT,
          sample_path TEXT,
          preview_sha256 TEXT,
          preview_count INTEGER NOT NULL DEFAULT 0,
          replay_count INTEGER NOT NULL DEFAULT 0,
          replay_pass_count INTEGER NOT NULL DEFAULT 0,
          replay_fail_count INTEGER NOT NULL DEFAULT 0,
          replay_message TEXT,
          enabled_rule_key TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          UNIQUE (run_id, rule_key)
        );
        CREATE INDEX IF NOT EXISTS ix_rule_agent_proposal_status ON rule_agent_proposal(status, updated_at);
        CREATE TABLE IF NOT EXISTS semantic_reasoning_run (
          reasoning_run_id TEXT PRIMARY KEY,
          idempotency_key TEXT NOT NULL UNIQUE,
          batch_id TEXT NOT NULL REFERENCES batch_run(batch_id),
          source_snapshot_id TEXT NOT NULL REFERENCES source_snapshot(source_snapshot_id),
          cluster_count INTEGER NOT NULL DEFAULT 0,
          sampled_count INTEGER NOT NULL DEFAULT 0,
          model TEXT NOT NULL,
          provider_base_url TEXT NOT NULL,
          profile_json TEXT NOT NULL DEFAULT '{}',
          status TEXT NOT NULL CHECK (status IN ('running','completed','failed')),
          error_message TEXT,
          created_at TEXT NOT NULL,
          finished_at TEXT,
          source_write INTEGER NOT NULL DEFAULT 0 CHECK (source_write = 0),
          formal_publication INTEGER NOT NULL DEFAULT 0 CHECK (formal_publication = 0)
        );
        CREATE INDEX IF NOT EXISTS ix_semantic_reasoning_run_created ON semantic_reasoning_run(created_at);
        CREATE TABLE IF NOT EXISTS semantic_reasoning_item (
          reasoning_item_id TEXT PRIMARY KEY,
          reasoning_run_id TEXT NOT NULL REFERENCES semantic_reasoning_run(reasoning_run_id),
          cluster_key TEXT NOT NULL,
          decision TEXT NOT NULL CHECK (decision IN ('propose_rule','keep_original','needs_review')),
          hypothesis TEXT NOT NULL DEFAULT '',
          evidence_json TEXT NOT NULL DEFAULT '{}',
          counterexamples_json TEXT NOT NULL DEFAULT '[]',
          candidate_rule_json TEXT NOT NULL DEFAULT '{}',
          confidence REAL NOT NULL DEFAULT 0,
          risk_level TEXT NOT NULL CHECK (risk_level IN ('low','medium','high')),
          required_checks_json TEXT NOT NULL DEFAULT '[]',
          created_at TEXT NOT NULL,
          UNIQUE (reasoning_run_id, cluster_key)
        );
        CREATE INDEX IF NOT EXISTS ix_semantic_reasoning_item_decision ON semantic_reasoning_item(decision, created_at);
        """
    )
    connection.commit()
    proposal_columns = {row[1] for row in connection.execute("PRAGMA table_info(rule_agent_proposal)").fetchall()}
    run_columns = {row[1] for row in connection.execute("PRAGMA table_info(rule_agent_run)").fetchall()}
    run_additive_columns = {
        "error_code": "TEXT",
        "retryable": "INTEGER NOT NULL DEFAULT 0 CHECK (retryable IN (0,1))",
        "attempt_count": "INTEGER NOT NULL DEFAULT 0",
        "fallback_used": "INTEGER NOT NULL DEFAULT 0 CHECK (fallback_used IN (0,1))",
    }
    for column, definition in run_additive_columns.items():
        if column not in run_columns:
            connection.execute(f"ALTER TABLE rule_agent_run ADD COLUMN {column} {definition}")
    proposal_additive_columns = {
        "discovery_filter_status": "TEXT NOT NULL DEFAULT 'eligible' CHECK (discovery_filter_status IN ('eligible','filtered'))",
        "discovery_filter_reason": "TEXT",
        "discovery_filtered_at": "TEXT",
        "evaluation_replay_id": "TEXT",
        "evaluation_count": "INTEGER NOT NULL DEFAULT 0",
        "evaluation_pass_count": "INTEGER NOT NULL DEFAULT 0",
        "evaluation_fail_count": "INTEGER NOT NULL DEFAULT 0",
        "agent_review_decision": "TEXT",
        "agent_review_confidence": "REAL NOT NULL DEFAULT 0",
        "agent_review_reason": "TEXT",
        "agent_review_version": "TEXT",
        "agent_reviewed_at": "TEXT",
    }
    for column, definition in proposal_additive_columns.items():
        if column not in proposal_columns:
            connection.execute(f"ALTER TABLE rule_agent_proposal ADD COLUMN {column} {definition}")
    legacy_filtered = connection.execute(
        """
        SELECT count(*)
        FROM rule_agent_proposal
        WHERE discovery_filter_status='eligible'
          AND status IN ('draft','previewed','replayed')
          AND expected_count<=0
          AND evidence_json LIKE '%\"matchedCount\": 0%'
        """
    ).fetchone()[0]
    if legacy_filtered:
        filtered_at = utc_now()
        connection.execute(
            """
            UPDATE rule_agent_proposal
            SET discovery_filter_status='filtered',
                discovery_filter_reason='legacy_zero_local_evidence',
                discovery_filtered_at=?,
                updated_at=?
            WHERE discovery_filter_status='eligible'
              AND status IN ('draft','previewed','replayed')
              AND expected_count<=0
              AND evidence_json LIKE '%\"matchedCount\": 0%'
            """,
            (filtered_at, filtered_at),
        )
        connection.execute(
            """
            INSERT INTO audit_event(entity_type,entity_id,event_type,actor,payload_json,event_at)
            VALUES (?,?,?,?,?,?)
            """,
            (
                "rule_agent",
                f"legacy-filter-{hashlib.sha256(filtered_at.encode()).hexdigest()[:12]}",
                "rule_agent_legacy_proposals_filtered",
                "rule-agent-migration",
                json.dumps(
                    {
                        "count": int(legacy_filtered),
                        "reason": "legacy_zero_local_evidence",
                        "sourceWrite": False,
                        "formalPublication": False,
                    },
                    ensure_ascii=False,
                ),
                filtered_at,
            ),
        )
    connection.commit()
    ensure_cleaning_schema(connection)


INITIAL_CLEANING_RULES = (
    {
        "rule_key": "semantic.separator.fullwidth_question_mark_to_space",
        "cleaning_type": "separator",
        "rule_label": "中间问号 → 空格",
        "action_label": "清洗",
        "is_cleaning": 1,
        "replay_id": "replay-question-separator-ffd10f4fc4e11852afbd",
        "rule_version": "question-separator-proposed-20260813-v1",
    },
    {
        "rule_key": "format.terminal_hyphen_trim",
        "cleaning_type": "terminal_hyphen",
        "rule_label": "末尾连字符清理",
        "action_label": "清洗",
        "is_cleaning": 1,
        "replay_id": "replay-terminal-hyphen-f1dd18e67ee8322b4617",
        "rule_version": "terminal-hyphen-proposed-20260813-v1",
    },
    {
        "rule_key": "policy.keep_original.leading_minus",
        "cleaning_type": "keep_original",
        "rule_label": "前导负号保留原文",
        "action_label": "保留原文",
        "is_cleaning": 0,
        "replay_id": "replay-ai-cluster-keep-original-1a3e057aedab3c077b82",
        "rule_version": "ai-cluster-keep-original-20260813-v1",
    },
)


def ensure_cleaning_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS cleaning_rule_registry (
          rule_key TEXT PRIMARY KEY,
          cleaning_type TEXT NOT NULL,
          rule_label TEXT NOT NULL,
          action_label TEXT NOT NULL,
          is_cleaning INTEGER NOT NULL CHECK (is_cleaning IN (0,1)),
          replay_id TEXT NOT NULL UNIQUE,
          rule_version TEXT NOT NULL,
          enabled INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0,1)),
          metadata_json TEXT NOT NULL DEFAULT '{}',
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS cleaning_run (
          cleaning_run_id TEXT PRIMARY KEY,
          rule_key TEXT NOT NULL REFERENCES cleaning_rule_registry(rule_key),
          replay_id TEXT NOT NULL,
          batch_id TEXT,
          source_type TEXT NOT NULL DEFAULT 'formal_queue',
          status TEXT NOT NULL CHECK (status IN ('draft','pending_approval','approved','published','failed')),
          candidate_count INTEGER NOT NULL DEFAULT 0,
          pending_count INTEGER NOT NULL DEFAULT 0,
          approved_count INTEGER NOT NULL DEFAULT 0,
          published_count INTEGER NOT NULL DEFAULT 0,
          preview_path TEXT,
          sample_path TEXT,
          archived INTEGER NOT NULL DEFAULT 0 CHECK (archived IN (0,1)),
          archived_at TEXT,
          archive_reason TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          UNIQUE (rule_key, replay_id)
        );
        CREATE INDEX IF NOT EXISTS ix_cleaning_rule_enabled ON cleaning_rule_registry(enabled, is_cleaning);
        CREATE INDEX IF NOT EXISTS ix_cleaning_run_status ON cleaning_run(status, updated_at);
        CREATE INDEX IF NOT EXISTS ix_cleaning_run_archived ON cleaning_run(archived, status, updated_at);
        """
    )
    # Keep the existing local database compatible with the generic task workflow.
    # These columns are additive metadata only; source and formal-result rows are
    # not rewritten by the migration.
    existing_columns = {row[1] for row in connection.execute("PRAGMA table_info(cleaning_run)").fetchall()}
    additive_columns = {
        "stage": "TEXT NOT NULL DEFAULT 'task'",
        "preview_id": "TEXT",
        "preview_sha256": "TEXT",
        "approval_idempotency_key": "TEXT",
        "publication_run_id": "TEXT",
        "backup_path": "TEXT",
        "last_error": "TEXT",
        "source_write": "INTEGER NOT NULL DEFAULT 0",
        "formal_publication": "INTEGER NOT NULL DEFAULT 0",
        "preview_rows": "INTEGER NOT NULL DEFAULT 0",
        "replay_rows": "INTEGER NOT NULL DEFAULT 0",
        "approval_key": "TEXT",
        "publication_key": "TEXT",
        "archived": "INTEGER NOT NULL DEFAULT 0 CHECK (archived IN (0,1))",
        "archived_at": "TEXT",
        "archive_reason": "TEXT",
    }
    for column, definition in additive_columns.items():
        if column not in existing_columns:
            connection.execute(f"ALTER TABLE cleaning_run ADD COLUMN {column} {definition}")
    registry_columns = {row[1] for row in connection.execute("PRAGMA table_info(cleaning_rule_registry)").fetchall()}
    if "metadata_json" not in registry_columns:
        connection.execute("ALTER TABLE cleaning_rule_registry ADD COLUMN metadata_json TEXT NOT NULL DEFAULT '{}'")
    now = utc_now()
    for item in INITIAL_CLEANING_RULES:
        connection.execute(
            """
            INSERT OR IGNORE INTO cleaning_rule_registry
              (rule_key,cleaning_type,rule_label,action_label,is_cleaning,replay_id,rule_version,enabled,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            (item["rule_key"], item["cleaning_type"], item["rule_label"], item["action_label"], item["is_cleaning"], item["replay_id"], item["rule_version"], 0, now, now),
        )
        connection.execute(
            """
            INSERT OR IGNORE INTO cleaning_run
              (cleaning_run_id,rule_key,replay_id,status,created_at,updated_at)
            VALUES (?,?,?,?,?,?)
            """,
            (f"cleaning-run-{item['replay_id']}", item["rule_key"], item["replay_id"], "draft", now, now),
        )
        # A registered rule is not active until its cleaning task has passed
        # replay and approval. Keep published historical rules active, but
        # automatically quarantine an existing pending task during migration.
        connection.execute(
            """
            UPDATE cleaning_rule_registry
            SET enabled=0,updated_at=?
            WHERE rule_key=? AND EXISTS (
              SELECT 1 FROM cleaning_run u
              WHERE u.rule_key=cleaning_rule_registry.rule_key
                AND u.replay_id=cleaning_rule_registry.replay_id
                AND u.status NOT IN ('approved','published')
            )
            """,
            (now, item["rule_key"]),
        )
    connection.commit()


def initialize_workflow_schema(connection: sqlite3.Connection) -> None:
    """Compatibility hook used until phase 3 moves migrations to startup."""
    ensure_review_sample_schema(connection)
    ensure_cleaning_schema(connection)


# Stage 3: schema work is startup-only. The initializer is passed explicitly
# to startup_workflow_migrations and is never attached to request connections.
configure_workflow_schema(None)


from app.migrations.workflow_schema import migrate_workflow_schema


@app.on_event("startup")
def startup_workflow_migrations() -> None:
    """Apply local workflow migrations once before serving requests."""
    connection = _core_raw_workflow_connection()
    try:
        migrate_workflow_schema(
            connection,
            ("workflow-review-sample-v1", ensure_review_sample_schema),
            ("workflow-cleaning-v1", ensure_cleaning_schema),
        )
    finally:
        connection.close()
        # Requests must never become an implicit migration trigger.
        configure_workflow_schema(None)


def duckdb_connection() -> Any:
    return _core_duckdb_connection()


def metadata_sqlite_connection() -> sqlite3.Connection:
    return _core_metadata_sqlite_connection()


def metadata_filters(
    concept_type: str | None,
    search: str | None,
    ai_category: str | None,
    semantic_status: str | None,
    source_schema: str | None,
) -> tuple[str, list[str]]:
    clauses = ["1=1"]
    parameters: list[str] = []
    if concept_type and concept_type != "all":
        clauses.append("concept_type = ?")
        parameters.append(concept_type)
    if ai_category and ai_category != "all":
        clauses.append("ai_category = ?")
        parameters.append(ai_category)
    if semantic_status and semantic_status != "all":
        clauses.append("semantic_status = ?")
        parameters.append(semantic_status)
    if source_schema and source_schema != "all":
        clauses.append("source_schemas LIKE ?")
        parameters.append(f"%{source_schema}%")
    if search and search.strip():
        term = f"%{search.strip()}%"
        clauses.append(
            "(semantic_id LIKE ? OR semantic_key LIKE ? OR canonical_name LIKE ? "
            "OR semantic_label_candidate LIKE ? OR description LIKE ? OR parent_or_table LIKE ?)"
        )
        parameters.extend([term] * 6)
    return " AND ".join(clauses), parameters


def metadata_row_payload(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "semanticId": row["semantic_id"],
        "dictionaryVersion": row["dictionary_version"],
        "conceptType": row["concept_type"],
        "semanticKey": row["semantic_key"],
        "canonicalName": row["canonical_name"],
        "semanticLabelCandidate": row["semantic_label_candidate"],
        "description": row["description"],
        "dataType": row["data_type"],
        "length": row["length"],
        "required": row["required"],
        "domainId": row["domain_id"],
        "parentOrTable": row["parent_or_table"],
        "sourceSchemas": row["source_schemas"],
        "crossSchemaStatus": row["cross_schema_status"],
        "aiCategory": row["ai_category"],
        "aiConfidence": row["ai_confidence"],
        "aiReason": row["ai_reason"],
        "semanticStatus": row["semantic_status"],
        "evidence": row["evidence"],
        "loadedAt": row["loaded_at"],
    }


# Metadata shaping is a domain service; keep the legacy names available to
# handlers while the remaining query bodies are migrated incrementally.
from app.domains.metadata.service import (
    metadata_filters as _metadata_filters,
    metadata_row_payload as _metadata_row_payload,
)

metadata_filters = _metadata_filters
metadata_row_payload = _metadata_row_payload


def metadata_summary() -> dict[str, Any]:
    """Return summary statistics from the local, versioned metadata layer."""
    connection = metadata_sqlite_connection()
    try:
        run = connection.execute(
            "SELECT run_id,dictionary_version,row_count,loaded_at,source_write,formal_publication "
            "FROM metadata_semantic_run ORDER BY loaded_at DESC LIMIT 1"
        ).fetchone()
        if run is None:
            raise HTTPException(status_code=503, detail="元数据语义结果库缺少运行批次")
        concept_types = connection.execute(
            "SELECT concept_type, count(*) AS count FROM metadata_semantic_dictionary "
            "GROUP BY concept_type ORDER BY count DESC, concept_type"
        ).fetchall()
        ai_categories = connection.execute(
            "SELECT COALESCE(NULLIF(ai_category,''),'unclassified') AS category, count(*) AS count "
            "FROM metadata_semantic_dictionary GROUP BY category ORDER BY count DESC, category"
        ).fetchall()
        semantic_statuses = connection.execute(
            "SELECT COALESCE(NULLIF(semantic_status,''),'unclassified') AS status, count(*) AS count "
            "FROM metadata_semantic_dictionary GROUP BY status ORDER BY count DESC, status"
        ).fetchall()
        finding_count = int(connection.execute("SELECT count(*) FROM metadata_validation_findings").fetchone()[0])
        finding_types = connection.execute(
            "SELECT finding_type, count(*) AS count FROM metadata_validation_findings "
            "GROUP BY finding_type ORDER BY count DESC, finding_type"
        ).fetchall()
        return {
            "resultVersion": run["dictionary_version"],
            "runId": run["run_id"],
            "loadedAt": run["loaded_at"],
            "total": int(run["row_count"]),
            "conceptTypes": [{"value": row["concept_type"], "count": int(row["count"])} for row in concept_types],
            "aiCategories": [{"value": row["category"], "count": int(row["count"])} for row in ai_categories],
            "semanticStatuses": [{"value": row["status"], "count": int(row["count"])} for row in semantic_statuses],
            "validation": {
                "findingCount": finding_count,
                "findingTypes": [{"value": row["finding_type"], "count": int(row["count"])} for row in finding_types],
            },
            "sourceWrite": False,
            "formalPublication": False,
            "source": "local-versioned-result-layer",
            "duckdbAvailable": METADATA_DUCKDB_DB.exists(),
        }
    finally:
        connection.close()


def metadata_catalog(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    concept_type: str | None = None,
    search: str | None = None,
    ai_category: str | None = None,
    semantic_status: str | None = None,
    source_schema: str | None = None,
) -> dict[str, Any]:
    """Search the approved local dictionary; no source database is opened."""
    connection = metadata_sqlite_connection()
    try:
        where_sql, parameters = metadata_filters(concept_type, search, ai_category, semantic_status, source_schema)
        total = int(connection.execute(
            f"SELECT count(*) FROM metadata_semantic_dictionary WHERE {where_sql}", parameters
        ).fetchone()[0])
        rows = connection.execute(
            f"SELECT * FROM metadata_semantic_dictionary WHERE {where_sql} "
            "ORDER BY concept_type, canonical_name, semantic_id LIMIT ? OFFSET ?",
            [*parameters, page_size, (page - 1) * page_size],
        ).fetchall()
        return {
            "items": [metadata_row_payload(row) for row in rows],
            "total": total,
            "page": page,
            "pageSize": page_size,
            "sourceWrite": False,
            "formalPublication": False,
        }
    finally:
        connection.close()


def metadata_catalog_detail(semantic_id: str) -> dict[str, Any]:
    connection = metadata_sqlite_connection()
    try:
        row = connection.execute(
            "SELECT * FROM metadata_semantic_dictionary WHERE semantic_id=?", (semantic_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="元数据语义记录不存在")
        related = connection.execute(
            "SELECT * FROM metadata_semantic_dictionary WHERE concept_type=? AND canonical_name=? "
            "AND semantic_id<>? ORDER BY semantic_key LIMIT 50",
            (row["concept_type"], row["canonical_name"], semantic_id),
        ).fetchall()
        return {
            "item": metadata_row_payload(row),
            "related": [metadata_row_payload(item) for item in related],
            "sourceWrite": False,
            "formalPublication": False,
        }
    finally:
        connection.close()


def metadata_export(
    concept_type: str | None = None,
    search: str | None = None,
    ai_category: str | None = None,
    semantic_status: str | None = None,
    source_schema: str | None = None,
) -> Response:
    connection = metadata_sqlite_connection()
    try:
        where_sql, parameters = metadata_filters(concept_type, search, ai_category, semantic_status, source_schema)
        rows = connection.execute(
            f"SELECT * FROM metadata_semantic_dictionary WHERE {where_sql} "
            "ORDER BY concept_type, canonical_name, semantic_id", parameters
        ).fetchall()
    finally:
        connection.close()
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    fields = [
        "semantic_id", "dictionary_version", "concept_type", "semantic_key", "canonical_name",
        "semantic_label_candidate", "description", "data_type", "length", "required", "domain_id",
        "parent_or_table", "source_schemas", "cross_schema_status", "ai_category", "ai_confidence",
        "ai_reason", "semantic_status", "evidence", "loaded_at",
    ]
    writer.writerow(fields)
    for row in rows:
        writer.writerow([row[field] for field in fields])
    return Response(
        content=output.getvalue().encode("utf-8-sig"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=metadata_semantic_dictionary.csv"},
    )


def latest_batch(connection: sqlite3.Connection) -> sqlite3.Row:
    row = connection.execute("SELECT * FROM batch_run ORDER BY started_at DESC LIMIT 1").fetchone()
    if row is None:
        raise HTTPException(status_code=503, detail="SQLite 中没有可用批次")
    return row


# The current queue records the evidence produced by the preview/replay stage
# as ``source_preview_and_replay``. Older batches use ``strong``. Both are
# valid local evidence for rule discovery; neither permits source writes or
# formal publication by itself.
RULE_AGENT_EVIDENCE_SQL = "c.evidence_level IN ('strong', 'source_preview_and_replay')"
RULE_AGENT_CONTEXT_SAMPLE_LIMIT = 10000


def rule_agent_profile(connection: sqlite3.Connection, sample_size: int = 120) -> dict[str, Any]:
    """Build a compact, source-read-only profile for the rule discovery agent."""
    batch = latest_batch(connection)
    eligible_where = f"""
        c.batch_id=? AND c.confidence='high' AND {RULE_AGENT_EVIDENCE_SQL}
        AND c.validator_status='candidate'
        AND c.review_state='pending' AND c.publication_state='unpublished'
        AND length(trim(c.original_description)) > 0
    """
    eligible_count = int(connection.execute(f"SELECT count(*) FROM semantic_candidate c WHERE {eligible_where}", (batch["batch_id"],)).fetchone()[0])
    changed_count = int(connection.execute(f"SELECT count(*) FROM semantic_candidate c WHERE {eligible_where} AND c.original_description<>c.candidate_description", (batch["batch_id"],)).fetchone()[0])
    # Read one deterministic pool, then stratify it in memory.  The previous
    # implementation issued one full candidate query per site, which made the
    # profile endpoint unusable for the current 397k-row batch.
    pool_limit = min(max(sample_size * 20, 2000), 10000)
    sample_pool = connection.execute(
        f"""
        SELECT c.candidate_id,c.original_description,c.candidate_description,
          d.site_id,d.asset_number,d.location_code,d.location_description,
          d.location_parent,d.classification_description
        FROM semantic_candidate c JOIN device_identity d ON d.device_id=c.device_id
        WHERE {eligible_where}
        ORDER BY c.candidate_id
        LIMIT ?
        """,
        (batch["batch_id"], pool_limit),
    ).fetchall()
    by_site: dict[str, list[sqlite3.Row]] = {}
    for row in sample_pool:
        by_site.setdefault(row["site_id"] or "", []).append(row)
    selected_rows: list[sqlite3.Row] = []
    if by_site:
        quota = max(1, sample_size // len(by_site))
        for site_id in sorted(by_site):
            selected_rows.extend(by_site[site_id][:quota])
        selected_ids = {row["candidate_id"] for row in selected_rows}
        if len(selected_rows) < sample_size:
            selected_rows.extend(
                row for row in sample_pool
                if row["candidate_id"] not in selected_ids
            )
    selected = selected_rows[:sample_size]
    sites = connection.execute(
        f"""
        SELECT d.site_id,count(*) AS count
        FROM semantic_candidate c JOIN device_identity d ON d.device_id=c.device_id
        WHERE c.batch_id=? AND c.confidence='high' AND {RULE_AGENT_EVIDENCE_SQL}
          AND c.validator_status='candidate'
          AND c.review_state='pending' AND c.publication_state='unpublished'
        GROUP BY d.site_id ORDER BY count DESC
        """,
        (batch["batch_id"],),
    ).fetchall()
    classifications = connection.execute(
        f"""
        SELECT COALESCE(NULLIF(trim(d.classification_description),''),'未分类') AS value,count(*) AS count
        FROM semantic_candidate c JOIN device_identity d ON d.device_id=c.device_id
        WHERE c.batch_id=? AND c.confidence='high' AND {RULE_AGENT_EVIDENCE_SQL}
          AND c.review_state='pending' AND c.publication_state='unpublished'
        GROUP BY value ORDER BY count DESC LIMIT 30
        """,
        (batch["batch_id"],),
    ).fetchall()
    examples = []
    for row in selected:
        examples.append({
            "candidateId": row["candidate_id"],
            "siteId": row["site_id"] or "",
            "assetNumber": row["asset_number"] or "",
            "originalDescription": row["original_description"] or "",
            "candidateDescription": row["candidate_description"] or "",
            "kks": row["location_code"] or "",
            "locationDescription": row["location_description"] or "",
            "locationParent": row["location_parent"] or "",
            "classificationDescription": row["classification_description"] or "",
        })
    local_rule_catalog, blocked_rule_catalog = build_rule_agent_local_catalog(
        connection,
        batch["batch_id"],
        max(6, RULE_AGENT_MAX_PROPOSALS * 4),
    )
    semantic_clusters = build_semantic_context_clusters(connection, batch["batch_id"], limit=40)
    return {
        "batchId": batch["batch_id"],
        "sourceSnapshotId": batch["source_snapshot_id"],
        "eligibleCount": eligible_count,
        "changedCount": changed_count,
        "sampleCount": len(examples),
        "sites": [{"siteId": row["site_id"], "count": int(row["count"])} for row in sites],
        "classifications": [{"value": row["value"], "count": int(row["count"])} for row in classifications],
        "examples": examples,
        "semanticClusters": semantic_clusters,
        "localRuleCatalog": local_rule_catalog,
        "blockedRuleCatalog": blocked_rule_catalog,
        "constraints": [
            "只提出设备描述清洗或统一语义规则",
            "不得臆造制造商、型号、容量或设备属性",
            "规则必须可确定性执行并可回放验证",
            "不得修改源库、候选记录或正式结果",
        ],
    }


def semantic_description_skeleton(value: str) -> str:
    """Create a grouping key without making semantic claims about a value."""
    text = unicodedata.normalize("NFKC", value or "")
    text = re.sub(r"[0-9０-９]+(?:[.．][0-9０-９]+)?", "<NUM>", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def build_semantic_context_clusters(
    connection: sqlite3.Connection,
    batch_id: str,
    limit: int = 40,
) -> list[dict[str, Any]]:
    """Summarize repeated description shapes with KKS/location/class context.

    Clusters are evidence for the agent only.  They do not imply that one
    description is preferred over another, and no cluster mutates a row.
    """
    eligible_count = int(
        connection.execute(
            f"""
            SELECT count(*)
            FROM semantic_candidate c
            WHERE c.batch_id=? AND c.confidence='high' AND {RULE_AGENT_EVIDENCE_SQL}
              AND c.validator_status='candidate' AND c.review_state='pending'
              AND c.publication_state='unpublished'
              AND length(trim(c.original_description)) > 0
            """,
            (batch_id,),
        ).fetchone()[0]
    )
    cache_key = (batch_id, limit, eligible_count)
    cached = _SEMANTIC_CONTEXT_CLUSTER_CACHE.get(cache_key)
    if cached is not None:
        return cached
    rows = connection.execute(
        f"""
        SELECT c.candidate_id,c.original_description,d.site_id,d.asset_number,
          d.location_code,d.location_description,d.location_parent,
          d.classification_description
        FROM semantic_candidate c JOIN device_identity d ON d.device_id=c.device_id
        WHERE c.batch_id=? AND c.confidence='high' AND {RULE_AGENT_EVIDENCE_SQL}
          AND c.validator_status='candidate' AND c.review_state='pending'
          AND c.publication_state='unpublished'
          AND length(trim(c.original_description)) > 0
        ORDER BY c.candidate_id
        LIMIT ?
        """,
        (batch_id, RULE_AGENT_CONTEXT_SAMPLE_LIMIT),
    ).fetchall()
    groups: dict[tuple[str, str], list[sqlite3.Row]] = {}
    for row in rows:
        skeleton = semantic_description_skeleton(row["original_description"] or "")
        classification = (row["classification_description"] or "").strip() or "__UNCLASSIFIED__"
        groups.setdefault((skeleton, classification), []).append(row)

    clusters: list[dict[str, Any]] = []
    for (skeleton, classification), group_members in groups.items():
        if len(group_members) < 2:
            continue
        members = group_members[:8]
        descriptions = list(dict.fromkeys((row["original_description"] or "") for row in members))
        site_ids = sorted({row["site_id"] or "" for row in members})
        parents = sorted({row["location_parent"] or "" for row in members if row["location_parent"]})
        kks_prefixes = sorted({(row["location_code"] or "")[:5] for row in members if row["location_code"]})
        examples = [
            {
                "candidateId": row["candidate_id"],
                "siteId": row["site_id"] or "",
                "assetNumber": row["asset_number"] or "",
                "before": row["original_description"] or "",
                "kks": row["location_code"] or "",
                "locationDescription": row["location_description"] or "",
                "locationParent": row["location_parent"] or "",
                "classificationDescription": row["classification_description"] or "",
            }
            for row in members[:4]
        ]
        clusters.append(
            {
                "clusterKey": hashlib.sha256(f"{skeleton}|{classification}".encode("utf-8")).hexdigest()[:16],
                "memberCount": len(group_members),
                "uniqueDescriptionCount": len({row["original_description"] or "" for row in group_members}),
                "descriptionSkeleton": skeleton,
                "classification": classification,
                "siteIds": site_ids[:20],
                "locationParents": parents[:20],
                "kksPrefixes": kks_prefixes[:20],
                "descriptions": descriptions[:8],
                "examples": examples,
            }
        )
    for item in clusters:
        item["patternKey"] = f"context.cluster.{item['clusterKey']}"
    clusters.sort(key=lambda item: (-item["uniqueDescriptionCount"], -item["memberCount"], item["clusterKey"]))
    result = clusters[:limit]
    _SEMANTIC_CONTEXT_CLUSTER_CACHE[cache_key] = result
    return result


def build_rule_agent_local_catalog(
    connection: sqlite3.Connection,
    batch_id: str,
    limit: int = 12,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Mine only executable, evidence-backed rule patterns from local data."""
    specs = [
        {
            "patternKey": "local.normalize.whitespace",
            "ruleKey": "format.normalize.whitespace",
            "title": "Normalize whitespace",
            "objective": "Collapse repeated, full-width, and boundary whitespace without changing equipment words.",
            "operation": "normalize",
            "condition": {},
            "parameters": {"mode": "whitespace"},
            "riskLevel": "low",
            "confidence": 0.98,
        },
        {
            "patternKey": "local.normalize.nfkc",
            "ruleKey": "format.normalize.nfkc",
            "title": "Normalize full-width and Unicode variants",
            "objective": "Normalize Unicode compatibility variants supported by NFKC.",
            "operation": "normalize",
            "condition": {},
            "parameters": {"mode": "nfkc"},
            "riskLevel": "low",
            "confidence": 0.96,
        },
        {
            "patternKey": "local.trim.boundary-whitespace",
            "ruleKey": "format.trim.boundary_whitespace",
            "title": "Trim boundary whitespace",
            "objective": "Remove whitespace before or after the original description.",
            "operation": "trim",
            "condition": {},
            "parameters": {},
            "riskLevel": "low",
            "confidence": 0.99,
        },
        {
            "patternKey": "local.replace.fullwidth-question-separator",
            "ruleKey": "format.separator.fullwidth_question_to_space",
            "title": "Replace full-width question separator",
            "objective": "Replace an internal full-width question separator with one space.",
            "operation": "replace",
            "condition": {"contains": "？"},
            "parameters": {"from": "？", "to": " "},
            "riskLevel": "medium",
            "confidence": 0.88,
        },
        {
            "patternKey": "local.replace.terminal-hyphen",
            "ruleKey": "format.terminal_hyphen_trim",
            "title": "Trim terminal hyphen",
            "objective": "Remove a trailing hyphen used as an incomplete separator.",
            "operation": "replace",
            "condition": {"suffix": "-"},
            "parameters": {"from": "-", "to": ""},
            "riskLevel": "low",
            "confidence": 0.94,
        },
        {
            "patternKey": "local.replace.uppercase-kv",
            "ruleKey": "format.unit.uppercase_kv_to_kv",
            "title": "Normalize KV unit capitalization",
            "objective": "Use kV for the unit capitalization when the source uses KV.",
            "operation": "replace",
            "condition": {"contains": "KV"},
            "parameters": {"from": "KV", "to": "kV"},
            "riskLevel": "low",
            "confidence": 0.9,
        },
    ]
    spec_by_pattern = {spec["patternKey"]: spec for spec in specs}

    def local_rule_match(value: str, pattern_key: str) -> int:
        spec = spec_by_pattern.get(pattern_key)
        if spec is None:
            return 0
        matched, transformed, _ = rule_agent_transform(
            value or "",
            spec["operation"],
            spec["condition"],
            spec["parameters"],
        )
        return int(matched and transformed != (value or ""))

    connection.create_function("rule_agent_local_match", 2, local_rule_match)
    catalog: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    for spec in specs:
        match_parameters = (batch_id, spec["patternKey"])
        matched_count = int(connection.execute(
            f"""
            SELECT count(*)
            FROM semantic_candidate c
            WHERE c.batch_id=? AND c.confidence='high' AND {RULE_AGENT_EVIDENCE_SQL}
              AND c.validator_status='candidate' AND c.review_state='pending'
              AND c.publication_state='unpublished' AND length(trim(c.original_description)) > 0
              AND rule_agent_local_match(c.original_description, ?)=1
            """,
            match_parameters,
        ).fetchone()[0])
        if matched_count == 0:
            continue
        rows = connection.execute(
            f"""
            SELECT c.candidate_id,c.original_description,d.site_id,d.asset_number,
                   d.location_code,d.location_description,d.location_parent,d.classification_description
            FROM semantic_candidate c JOIN device_identity d ON d.device_id=c.device_id
            WHERE c.batch_id=? AND c.confidence='high' AND {RULE_AGENT_EVIDENCE_SQL}
              AND c.validator_status='candidate' AND c.review_state='pending'
              AND c.publication_state='unpublished' AND length(trim(c.original_description)) > 0
              AND rule_agent_local_match(c.original_description, ?)=1
            ORDER BY c.candidate_id
            LIMIT 5
            """,
            match_parameters,
        ).fetchall()
        targets: list[dict[str, Any]] = []
        for row in rows:
            original = row["original_description"] or ""
            matched, transformed, reason = rule_agent_transform(
                original,
                spec["operation"],
                spec["condition"],
                spec["parameters"],
            )
            if not matched or transformed == original:
                continue
            targets.append(
                {
                    "before": original,
                    "after": transformed,
                    "candidateId": row["candidate_id"],
                    "siteId": row["site_id"] or "",
                    "assetNumber": row["asset_number"] or "",
                    "kks": row["location_code"] or "",
                    "locationDescription": row["location_description"] or "",
                    "locationParent": row["location_parent"] or "",
                    "classificationDescription": row["classification_description"] or "",
                    "matchReason": reason,
                }
            )
        if not targets:
            continue
        catalog_item = {
            **spec,
            "matchedCount": matched_count,
            "sampleCount": len(targets),
            "examples": targets,
            "evidenceSource": "local_candidate_scan",
            "catalogVersion": RULE_AGENT_CATALOG_VERSION,
        }
        replay_gate = evaluate_rule_agent_catalog_spec(connection, catalog_item)
        catalog_item["evaluationCount"] = replay_gate["evaluationCount"]
        catalog_item["evaluationPassCount"] = replay_gate["evaluationPassCount"]
        catalog_item["evaluationFailCount"] = replay_gate["evaluationFailCount"]
        if replay_gate["status"] != "passed":
            blocked.append(
                {
                    "patternKey": spec["patternKey"],
                    "matchedCount": len(targets),
                    "sampleCount": min(5, len(targets)),
                    "reason": "active_evaluation_regression",
                    "evaluationCount": replay_gate["evaluationCount"],
                    "evaluationFailCount": replay_gate["evaluationFailCount"],
                    "failures": replay_gate["failures"],
                }
            )
            continue
        catalog.append(catalog_item)
    catalog.sort(key=lambda item: (-int(item["matchedCount"]), str(item["patternKey"])))
    return catalog[:limit], blocked


def rule_agent_payload(profile: dict[str, Any]) -> str:
    compact_profile = dict(profile)
    compact_profile["examples"] = profile.get("examples", [])[: min(len(profile.get("examples", [])), 5)]
    compact_profile["sites"] = profile.get("sites", [])[:20]
    compact_profile["classifications"] = profile.get("classifications", [])[:20]
    compact_profile["localRuleCatalog"] = [
        {**item, "examples": item.get("examples", [])[:3]}
        for item in profile.get("localRuleCatalog", [])
    ]
    compact_profile["blockedRuleCatalog"] = profile.get("blockedRuleCatalog", [])[:10]
    compact_profile["semanticClusters"] = [
        {**item, "examples": item.get("examples", [])[:3], "descriptions": item.get("descriptions", [])[:6]}
        for item in profile.get("semanticClusters", [])[:30]
    ]
    compact_profile["contextPatternKeys"] = [
        item.get("patternKey") for item in compact_profile["semanticClusters"] if item.get("patternKey")
    ]
    return json.dumps({
        "task": f"从设备描述数据中发现最多 {RULE_AGENT_MAX_PROPOSALS} 条可重复、可解释、可回放的清洗规则",
        "policy": [
            "Select a patternKey from profile.localRuleCatalog, or select an exact value from profile.contextPatternKeys when semanticClusters provide repeated evidence. Never return placeholders such as context.stable-name.",
            "Context semantic proposals must use replace only, with explicit parameters.from/to; normalize and trim are reserved for the local format catalog.",
            "For context proposals, replace must include explicit parameters.from/to and scope may use sites, classifications, locationParents, locationCodes, or kksPrefixes.",
            "The local executor discards proposals with zero local matches or no examples; every proposal is only a draft.",
            "AI may provide title, objective, reason, confidence, and risk assessment; return JSON only.",
        ],
        "output_schema": {
            "proposals": [{
                "patternKey": "local.normalize.whitespace|context.cluster.<exact-key-from-contextPatternKeys>",
                "ruleKey": "stable.dot.separated.key",
                "title": "规则名称",
                "objective": "规则目的",
                "operation": "replace|normalize|trim|keep_original|block",
                "condition": {"descriptionPattern": "可解释条件", "context": "可选上下文条件"},
                "parameters": {"from": "", "to": ""},
                "scope": {"sites": [], "classifications": [], "locationParents": [], "locationCodes": [], "kksPrefixes": []},
                "evidence": {"reason": "", "matchedCount": 0},
                "examples": [{"before": "", "after": "", "candidateId": ""}],
                "expectedCount": 0,
                "confidence": 0.0,
                "riskLevel": "low|medium|high"
            }]
        },
        "output_schema": {
            "proposals": [{
                "patternKey": "local.normalize.whitespace|context.cluster.<exact-key-from-contextPatternKeys>",
                "ruleKey": "stable.dot.separated.key",
                "title": "rule title",
                "objective": "rule objective",
                "operation": "replace",
                "condition": {"contains": "", "prefix": "", "suffix": ""},
                "parameters": {"from": "", "to": ""},
                "scope": {"sites": [], "classifications": [], "locationParents": [], "locationCodes": [], "kksPrefixes": []},
                "evidence": {"reason": "", "matchedCount": 0},
                "examples": [{"before": "", "after": "", "candidateId": ""}],
                "expectedCount": 0,
                "confidence": 0.0,
                "riskLevel": "low|medium|high"
            }]
        },
        "profile": compact_profile,
    }, ensure_ascii=False)


def invoke_rule_agent(profile: dict[str, Any]) -> list[dict[str, Any]]:
    try:
        text = invoke_rule_agent_completion(
            [
                {"role": "system", "content": "Use exact localRuleCatalog patternKey values, or an exact contextPatternKeys value when semanticClusters provide repeated evidence. Never return placeholders such as context.stable-name. Context semantic proposals must use replace only with explicit parameters.from/to and scope. Never invent regex or unsupported parameters. Return JSON only."},
                {"role": "system", "content": "你是电厂设备描述规则发现智能体。必须只输出一个 JSON 对象，格式为 {\"proposals\":[...]}，不要输出解释、前后缀或 Markdown。"},
                {"role": "user", "content": rule_agent_payload(profile)},
            ],
            "规则智能体",
        )
        parsed = parse_rule_agent_json(text, "规则智能体")
    except RuleAgentCallError as exc:
        status = 503 if exc.code == "agent_not_configured" else 502
        raise HTTPException(status_code=status, detail=rule_agent_failure_detail(exc)) from exc
    if isinstance(parsed, dict):
        proposals = parsed.get("proposals", [])
    elif isinstance(parsed, list):
        proposals = parsed
    else:
        proposals = []
    if not isinstance(proposals, list):
        raise HTTPException(status_code=502, detail="规则智能体返回格式不正确")
    return [item for item in proposals if isinstance(item, dict)][:RULE_AGENT_MAX_PROPOSALS]


def semantic_reasoning_payload(clusters: list[dict[str, Any]]) -> str:
    compact_clusters = []
    for cluster in clusters:
        compact_clusters.append(
            {
                **cluster,
                "examples": cluster.get("examples", [])[:4],
                "descriptions": cluster.get("descriptions", [])[:8],
            }
        )
    return json.dumps(
        {
            "task": "Reason over equipment-description clusters and context; do not rewrite individual records.",
            "policy": [
                "Use only evidence present in the cluster descriptions, KKS/location parent, classification, site, and examples.",
                "Do not infer manufacturer, model, capacity, equipment type, or a preferred term without explicit evidence.",
                "A candidate rule must be an explicit replace from/to mapping; never use regex or broad normalization.",
                "Return keep_original or needs_review when the cluster shows only legitimate numeric or identity variation.",
                "Every proposed rule is a draft for local matching, replay, preview, and human confirmation; never publish.",
            ],
            "output_schema": {
                "reasoning": [
                    {
                        "clusterKey": "context.cluster.<exact-key>",
                        "decision": "propose_rule|keep_original|needs_review",
                        "hypothesis": "short evidence-based explanation",
                        "evidence": {"observations": [], "supportingExamples": []},
                        "counterexamples": [],
                        "candidateRule": {
                            "ruleKey": "context.cluster.<exact-key>.term",
                            "operation": "replace",
                            "condition": {"contains": ""},
                            "parameters": {"from": "", "to": ""},
                            "scope": {"sites": [], "classifications": [], "locationParents": [], "locationCodes": [], "kksPrefixes": []},
                        },
                        "confidence": 0.0,
                        "riskLevel": "medium|high",
                        "requiredChecks": [],
                    }
                ]
            },
            "clusters": compact_clusters,
        },
        ensure_ascii=False,
    )


def invoke_semantic_reasoning_agent(clusters: list[dict[str, Any]]) -> list[dict[str, Any]]:
    try:
        text = invoke_rule_agent_completion(
            [
                {
                    "role": "system",
                    "content": "You are a conservative semantic reasoning agent for power-plant equipment descriptions. Return JSON only. Reason about clusters, never modify source data.",
                },
                {"role": "user", "content": semantic_reasoning_payload(clusters)},
            ],
            "语义推理智能体",
        )
        parsed = parse_rule_agent_json(text, "语义推理智能体")
    except RuleAgentCallError as exc:
        status = 503 if exc.code == "agent_not_configured" else 502
        raise HTTPException(status_code=status, detail=rule_agent_failure_detail(exc)) from exc
    items = parsed.get("reasoning", []) if isinstance(parsed, dict) else parsed
    if not isinstance(items, list):
        raise HTTPException(status_code=502, detail="语义推理智能体返回结构不正确")
    return [item for item in items if isinstance(item, dict)][:30]


# Stage 2 prompt binding. Prompts are pure functions and do not depend on the
# FastAPI app, database connections, or source systems.
from app.agent.prompts import (
    rule_agent_payload as _rule_agent_payload,
    semantic_reasoning_payload as _semantic_reasoning_payload,
)

rule_agent_payload = _rule_agent_payload
semantic_reasoning_payload = _semantic_reasoning_payload


def canonicalize_semantic_reasoning(
    items: list[dict[str, Any]],
    clusters: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    cluster_aliases: dict[str, str] = {}
    for cluster in clusters:
        cluster_key = str(cluster.get("clusterKey") or "")
        if not cluster_key:
            continue
        cluster_aliases[cluster_key] = cluster_key
        cluster_aliases[f"context.cluster.{cluster_key}"] = cluster_key
        if cluster.get("patternKey"):
            cluster_aliases[str(cluster["patternKey"])] = cluster_key
    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        raw_cluster_key = str(item.get("clusterKey") or item.get("clusterId") or "").strip()
        cluster_key = cluster_aliases.get(raw_cluster_key, "")
        if not cluster_key or cluster_key in seen:
            continue
        seen.add(cluster_key)
        decision = str(item.get("decision") or "needs_review").strip().lower()
        decision = {
            "propose": "propose_rule",
            "propose_rule": "propose_rule",
            "keep": "keep_original",
            "preserve": "keep_original",
            "keep_original": "keep_original",
            "review": "needs_review",
            "needs_review": "needs_review",
        }.get(decision, "needs_review")
        if decision not in {"propose_rule", "keep_original", "needs_review"}:
            decision = "needs_review"
        try:
            confidence = max(0.0, min(1.0, float(item.get("confidence") or 0)))
        except (TypeError, ValueError):
            confidence = 0.0
        risk = str(item.get("riskLevel") or "medium").strip().lower()
        if risk not in {"low", "medium", "high"}:
            risk = "medium"
        if decision == "propose_rule":
            risk = "high" if risk == "high" else "medium"
        candidate = item.get("candidateRule") or item.get("ruleDraft") or item.get("rule")
        candidate = candidate if isinstance(candidate, dict) else {}
        operation = str(candidate.get("operation") or "").strip().lower()
        condition = candidate.get("condition") if isinstance(candidate.get("condition"), dict) else {}
        parameters = candidate.get("parameters") if isinstance(candidate.get("parameters"), dict) else {}
        scope = candidate.get("scope") if isinstance(candidate.get("scope"), dict) else {}
        required_checks = item.get("requiredChecks") if isinstance(item.get("requiredChecks"), list) else []
        required_checks = [str(value)[:300] for value in required_checks[:20]]
        if decision == "propose_rule":
            source = str(parameters.get("from") or "")
            target = str(parameters.get("to") if parameters.get("to") is not None else "")
            allowed_condition = {"contains", "prefix", "suffix"}
            allowed_scope = {"sites", "classifications", "locationParents", "locationCodes", "kksPrefixes"}
            valid = (
                operation == "replace"
                and bool(source)
                and source != target
                and len(source) <= 80
                and len(target) <= 80
                and all(key in allowed_condition for key in condition)
                and all(key in allowed_scope for key in scope)
            )
            if not valid:
                decision = "needs_review"
                required_checks.append("candidate_rule_dsl_invalid")
            else:
                candidate = {
                    "ruleKey": str(candidate.get("ruleKey") or f"context.{cluster_key}.term")[:180],
                    "operation": "replace",
                    "condition": condition,
                    "parameters": {"from": source, "to": target},
                    "scope": scope,
                }
        evidence = item.get("evidence") if isinstance(item.get("evidence"), dict) else {}
        if isinstance(item.get("evidence"), str):
            evidence = {"reason": str(item["evidence"])[:2000]}
        counterexamples = item.get("counterexamples") if isinstance(item.get("counterexamples"), list) else []
        output.append(
            {
                "clusterKey": cluster_key,
                "decision": decision,
                "hypothesis": str(item.get("hypothesis") or "")[:2000],
                "evidence": evidence,
                "counterexamples": counterexamples[:20],
                "candidateRule": candidate if decision == "propose_rule" else {},
                "confidence": confidence,
                "riskLevel": risk,
                "requiredChecks": required_checks,
            }
        )
    return output


def semantic_reasoning_item_payload(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "reasoningItemId": row["reasoning_item_id"],
        "runId": row["reasoning_run_id"],
        "clusterKey": row["cluster_key"],
        "decision": row["decision"],
        "hypothesis": row["hypothesis"],
        "evidence": json.loads(row["evidence_json"] or "{}"),
        "counterexamples": json.loads(row["counterexamples_json"] or "[]"),
        "candidateRule": json.loads(row["candidate_rule_json"] or "{}"),
        "confidence": float(row["confidence"] or 0),
        "riskLevel": row["risk_level"],
        "requiredChecks": json.loads(row["required_checks_json"] or "[]"),
        "createdAt": row["created_at"],
    }


CANDIDATE_AGENT_REVIEW_VERSION = "candidate-keep-original-agent-20260817-v2"
CANDIDATE_AGENT_DEFAULT_BATCH_SIZE = 100
CANDIDATE_AGENT_MAX_BATCH_SIZE = 500
CANDIDATE_AGENT_EVIDENCE_LEVELS = ("strong", "source_preview_and_replay")


def candidate_agent_where(
    batch_id: str,
    *,
    include_previously_audited: bool = False,
    candidate_ids: list[str] | None = None,
) -> tuple[str, list[Any]]:
    """Return the local safety gate for model-assisted candidate review.

    This gate deliberately selects only high-quality, replayed candidates.  It
    never selects blocked/contradictory records and it does not let an agent
    invent a replacement description.
    """
    where = [
        "c.batch_id=?",
        "c.review_state='pending'",
        "c.publication_state='unpublished'",
        "c.validator_status='candidate'",
        "c.confidence='high'",
        "c.evidence_level IN ('strong','source_preview_and_replay')",
        "length(trim(c.original_description)) > 0",
        "length(trim(c.candidate_description)) > 0",
        "c.reason_codes_json NOT LIKE '%CONFLICT%'",
        "c.reason_codes_json NOT LIKE '%BLOCK%'",
    ]
    parameters: list[Any] = [batch_id]
    if candidate_ids:
        marks = ",".join("?" for _ in candidate_ids)
        where.append(f"c.candidate_id IN ({marks})")
        parameters.extend(candidate_ids)
    elif not include_previously_audited:
        where.append(
            "NOT EXISTS ("
            "SELECT 1 FROM audit_event ae "
            "WHERE ae.entity_type='candidate' "
            "AND ae.entity_id=c.candidate_id "
            "AND ae.event_type IN ('candidate_agent_review_isolated','candidate_agent_review_deferred')"
            ")"
        )
    return " AND ".join(where), parameters


def candidate_agent_preview(connection: sqlite3.Connection, batch: sqlite3.Row, batch_size: int) -> dict[str, Any]:
    where_sql, parameters = candidate_agent_where(batch["batch_id"])
    pending_count = int(connection.execute(
        "SELECT count(*) FROM semantic_candidate WHERE batch_id=? AND review_state='pending'",
        (batch["batch_id"],),
    ).fetchone()[0])
    eligible_count = int(connection.execute(
        f"SELECT count(*) FROM semantic_candidate c WHERE {where_sql}", parameters,
    ).fetchone()[0])
    isolated_count = max(0, pending_count - eligible_count)
    site_rows = connection.execute(
        f"""
        SELECT d.site_id, count(*) AS count
        FROM semantic_candidate c
        JOIN device_identity d ON d.device_id=c.device_id
        WHERE {where_sql}
        GROUP BY d.site_id
        ORDER BY count DESC, d.site_id
        LIMIT 12
        """,
        parameters,
    ).fetchall()
    audited_count = int(connection.execute(
        """
        SELECT count(DISTINCT ae.entity_id)
        FROM audit_event ae
        JOIN semantic_candidate c ON c.candidate_id=ae.entity_id
        WHERE c.batch_id=? AND ae.entity_type='candidate'
          AND ae.event_type IN ('candidate_agent_review_isolated','candidate_agent_review_deferred')
        """,
        (batch["batch_id"],),
    ).fetchone()[0])
    return {
        "batchId": batch["batch_id"],
        "pendingCount": pending_count,
        "eligibleCount": eligible_count,
        "nextBatchSize": min(batch_size, eligible_count),
        "isolatedCount": isolated_count,
        "auditedCount": audited_count,
        "siteCounts": [{"siteId": row["site_id"] or "未标识", "count": int(row["count"])} for row in site_rows],
        "batchSize": batch_size,
        "batchSizeOptions": [50, 100, 200, 500],
        "selectionStrategy": "round_robin_by_SITEID",
        "policyVersion": CANDIDATE_AGENT_REVIEW_VERSION,
        "model": RULE_AGENT_MODEL,
        "configured": bool(RULE_AGENT_API_KEY and RULE_AGENT_BASE_URL),
        "sourceWrite": False,
        "formalPublication": False,
        "workflow": ["高质量门禁", "AI 保守审阅", "自动通过或隔离", "人工复核隔离项", "独立审批与发布"],
        "gates": [
            "仅选择 high + candidate + 未发布记录",
            "证据为 strong 或 source_preview_and_replay",
            "排除 CONFLICT/BLOCK 原因码",
            "模型只能保留原文或进入隔离，不生成新描述",
            "不写 MaxiEAM，不进入正式发布层",
        ],
    }


def invoke_pending_candidate_review(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    """Ask the model to audit pending candidates; it cannot write or publish."""
    records: list[dict[str, Any]] = []
    for row in rows:
        try:
            reason_codes = json.loads(row["reason_codes_json"] or "[]")
        except json.JSONDecodeError:
            reason_codes = []
        records.append(
            {
                "candidateId": row["candidate_id"],
                "siteId": row["site_id"] or "",
                "assetNumber": row["asset_number"] or "",
                "originalDescription": row["original_description"] or "",
                "candidateDescription": row["candidate_description"] or "",
                "semanticAction": row["semantic_action"] or "",
                "confidence": row["confidence"] or "",
                "evidenceLevel": row["evidence_level"] or "",
                "validatorStatus": row["validator_status"] or "",
                "reasonCodes": reason_codes,
                "kks": row["location_code"] or "",
                "locationDescription": row["location_description"] or "",
                "locationParent": row["location_parent"] or "",
                "classificationDescription": row["classification_description"] or "",
            }
        )
    payload = {
        "task": "Audit pending power-plant equipment descriptions. Preserve the original description when it is the safest supported outcome.",
        "outputSchema": {
            "reviews": [
                {
                    "candidateId": "",
                    "decision": "approve_keep_original|needs_review|reject",
                    "confidence": 0.0,
                    "reason": "",
                    "risk": "low|medium|high",
                }
            ]
        },
        "policy": [
            "Use only the supplied original description and explicit context fields.",
            "Never invent manufacturer, model, capacity, equipment type, KKS, location, or classification.",
            "approve_keep_original only when the original is safe to retain, there is no conflict or block reason, and no candidate rewrite is justified.",
            "A leading minus may be a meaningful negative elevation or voltage sign; do not remove it.",
            "Return needs_review when evidence is basic, context is insufficient, or any semantic conflict is present.",
            "Return JSON only. Do not propose a replacement description.",
        ],
        "records": records,
    }
    try:
        text = invoke_rule_agent_completion(
            [
                {"role": "system", "content": "You are a conservative equipment-description audit agent. Return valid JSON only. Never modify source data."},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            "设备候选审核智能体",
        )
        parsed = parse_rule_agent_json(text, "设备候选审核智能体")
    except RuleAgentCallError as exc:
        status = 503 if exc.code == "agent_not_configured" else 502
        raise HTTPException(status_code=status, detail=rule_agent_failure_detail(exc)) from exc
    reviews = parsed.get("reviews", []) if isinstance(parsed, dict) else parsed
    return [item for item in reviews if isinstance(item, dict)] if isinstance(reviews, list) else []


def canonicalize_pending_candidate_reviews(
    raw_items: list[dict[str, Any]], rows: list[sqlite3.Row]
) -> list[dict[str, Any]]:
    allowed = {str(row["candidate_id"]): row for row in rows}
    by_candidate: dict[str, dict[str, Any]] = {}
    aliases = {
        "approve_keep_original": "approve_keep_original",
        "keep_original": "approve_keep_original",
        "approve": "approve_keep_original",
        "approved": "approve_keep_original",
        "pass": "approve_keep_original",
        "needs_review": "needs_review",
        "review": "needs_review",
        "reject": "reject",
        "rejected": "reject",
    }
    for item in raw_items:
        candidate_id = str(item.get("candidateId") or item.get("candidate_id") or "").strip()
        if candidate_id not in allowed or candidate_id in by_candidate:
            continue
        decision = aliases.get(str(item.get("decision") or "needs_review").strip().lower(), "needs_review")
        try:
            confidence = max(0.0, min(1.0, float(item.get("confidence") or 0)))
        except (TypeError, ValueError):
            confidence = 0.0
        by_candidate[candidate_id] = {
            "candidateId": candidate_id,
            "agentDecision": decision,
            "confidence": confidence,
            "reason": str(item.get("reason") or "agent did not provide a reason").strip()[:2000],
            "risk": str(item.get("risk") or "medium").strip().lower(),
        }
    results: list[dict[str, Any]] = []
    for row in rows:
        candidate_id = str(row["candidate_id"])
        item = by_candidate.get(
            candidate_id,
            {"candidateId": candidate_id, "agentDecision": "needs_review", "confidence": 0.0, "reason": "agent returned no decision", "risk": "high"},
        )
        reason_codes = []
        try:
            reason_codes = json.loads(row["reason_codes_json"] or "[]")
        except json.JSONDecodeError:
            pass
        blocking_reason = any("CONFLICT" in str(code).upper() or "BLOCK" in str(code).upper() for code in reason_codes)
        original = row["original_description"] or ""
        candidate = row["candidate_description"] or ""
        leading_minus_preservation = (
            str(row["semantic_action"] or "") == "PRESERVE_SOURCE_DESCRIPTION"
            and original.startswith("-")
            and candidate == original[1:]
        )
        equivalent = original == candidate or original.strip() == candidate.strip()
        local_safe = (
            row["validator_status"] == "candidate"
            and row["confidence"] == "high"
            and row["evidence_level"] in CANDIDATE_AGENT_EVIDENCE_LEVELS
            and not blocking_reason
            and bool(original.strip())
            and bool(candidate.strip())
            and (
                "replay_passed" in reason_codes
                and "source_identity_verified" in reason_codes
            )
        )
        approved = item["agentDecision"] == "approve_keep_original" and item["confidence"] >= 0.85 and local_safe
        item["decision"] = "approved" if approved else "needs_review"
        item["localGate"] = "passed" if local_safe else "blocked"
        results.append(item)
    return results


def context_rule_spec_from_agent(item: dict[str, Any], pattern_key: str) -> dict[str, Any] | None:
    """Validate a context proposal before it reaches the local executor."""
    if not re.fullmatch(r"context\.[a-z0-9][a-z0-9_.-]{2,120}", pattern_key):
        return None
    operation = str(item.get("operation") or "").strip().lower()
    if operation != "replace":
        return None
    condition = item.get("condition") if isinstance(item.get("condition"), dict) else {}
    allowed_conditions = {"contains", "descriptionContains", "prefix", "suffix"}
    if any(key not in allowed_conditions for key in condition):
        return None
    parameters = item.get("parameters") if isinstance(item.get("parameters"), dict) else {}
    if operation == "replace":
        source = str(parameters.get("from") or parameters.get("source") or "")
        if not source or len(source) > 80:
            return None
        target = str(parameters.get("to") if parameters.get("to") is not None else parameters.get("target") or "")
        if len(target) > 80 or source == target:
            return None
        parameters = {"from": source, "to": target}
    elif operation == "normalize":
        mode = str(parameters.get("mode") or "").strip().lower()
        if mode not in {"nfkc", "whitespace", "nfkc_whitespace"}:
            return None
        parameters = {"mode": mode}
    else:
        parameters = {}
    raw_scope = item.get("scope") if isinstance(item.get("scope"), dict) else {}
    allowed_scope = {"sites", "siteIds", "classifications", "classificationIds", "locationParents", "locationParent", "locationCodes", "locationCode", "kksPrefixes", "kksPrefix"}
    if any(key not in allowed_scope for key in raw_scope):
        return None
    scope = {key: value for key, value in raw_scope.items() if value not in (None, [], "")}
    if not condition and not scope:
        return None
    return {
        "patternKey": pattern_key,
        "ruleKey": re.sub(r"[^a-z0-9_.-]+", "_", str(item.get("ruleKey") or pattern_key).lower())[:180],
        "title": str(item.get("title") or pattern_key)[:200],
        "objective": str(item.get("objective") or "Context-scoped semantic rule draft")[:1000],
        "operation": operation,
        "condition": condition,
        "parameters": parameters,
        "scope": scope,
        "riskLevel": "medium",
        "confidence": 0.80,
        "matchedCount": 0,
        "sampleCount": 0,
        "examples": [],
        "evidenceSource": "semantic_context_cluster_agent",
        "catalogVersion": "semantic-context-dsl-20260814-v1",
    }


def canonicalize_rule_agent_proposals(
    proposals: list[dict[str, Any]],
    catalog: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool]:
    """Bind AI output to the locally mined executable rule catalog."""
    catalog_by_key = {str(item["patternKey"]): item for item in catalog}
    selected: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    used: set[str] = set()
    for item in proposals:
        pattern_key = str(item.get("patternKey") or "").strip()
        spec = catalog_by_key.get(pattern_key)
        if spec is None and pattern_key.startswith("context."):
            spec = context_rule_spec_from_agent(item, pattern_key)
        if spec is None:
            rejected.append(
                {
                    "patternKey": pattern_key,
                    "reason": "context_dsl_validation_failed" if pattern_key.startswith("context.") else "unsupported_or_missing_pattern_key",
                    "operation": item.get("operation"),
                    "condition": item.get("condition"),
                    "parameters": item.get("parameters"),
                    "scope": item.get("scope"),
                }
            )
            continue
        if pattern_key in used:
            rejected.append({"patternKey": pattern_key, "reason": "duplicate_pattern_key"})
            continue
        used.add(pattern_key)
        try:
            model_confidence = max(0.0, min(1.0, float(item.get("confidence") or 0)))
        except (TypeError, ValueError):
            model_confidence = 0.0
        risk_order = {"low": 0, "medium": 1, "high": 2}
        model_risk = str(item.get("riskLevel") or spec["riskLevel"]).lower()
        if model_risk not in risk_order:
            model_risk = spec["riskLevel"]
        risk = max((spec["riskLevel"], model_risk), key=lambda value: risk_order[value])
        ai_evidence = item.get("evidence") if isinstance(item.get("evidence"), dict) else {}
        selected.append(
            {
                **item,
                "patternKey": pattern_key,
                "ruleKey": spec["ruleKey"],
                "title": str(item.get("title") or spec["title"])[:200],
                "objective": str(item.get("objective") or spec["objective"])[:1000],
                "operation": spec["operation"],
                "condition": spec["condition"],
                "parameters": spec["parameters"],
                "scope": {},
                "evidence": {
                    "reason": str(ai_evidence.get("reason") or "")[:1000],
                    "matchedCount": spec["matchedCount"],
                    "sampleCount": spec["sampleCount"],
                    "measuredFromLocalPreview": True,
                    "evidenceSource": spec["evidenceSource"],
                    "catalogVersion": spec["catalogVersion"],
                    "patternKey": pattern_key,
                },
                "examples": spec["examples"],
                "expectedCount": spec["matchedCount"],
                "confidence": min(model_confidence, float(spec["confidence"])) if model_confidence else 0.0,
                "riskLevel": risk,
            }
        )

    fallback_used = False
    if not selected:
        fallback_used = bool(catalog)
        for spec in catalog[:RULE_AGENT_MAX_PROPOSALS]:
            selected.append(
                {
                    "patternKey": spec["patternKey"],
                    "ruleKey": spec["ruleKey"],
                    "title": spec["title"],
                    "objective": spec["objective"],
                    "operation": spec["operation"],
                    "condition": spec["condition"],
                    "parameters": spec["parameters"],
                    "scope": {},
                    "evidence": {
                        "reason": "local deterministic catalog fallback",
                        "matchedCount": spec["matchedCount"],
                        "sampleCount": spec["sampleCount"],
                        "measuredFromLocalPreview": True,
                        "evidenceSource": spec["evidenceSource"],
                        "catalogVersion": spec["catalogVersion"],
                        "patternKey": spec["patternKey"],
                    },
                    "examples": spec["examples"],
                    "expectedCount": spec["matchedCount"],
                    "confidence": float(spec["confidence"]),
                    "riskLevel": spec["riskLevel"],
                    "agentGenerated": False,
                }
            )
    return selected[:RULE_AGENT_MAX_PROPOSALS], rejected, fallback_used


AGENT_REVIEW_VERSION = "rule-agent-review-20260814-v1"
RULE_AGENT_DISCOVERY_FILTER_VERSION = "rule-agent-local-evidence-20260814-v1"
RULE_AGENT_MIN_MATCH_COUNT = 1
RULE_AGENT_MIN_EVIDENCE_SAMPLES = 1


def invoke_rule_agent_review(proposals: list[sqlite3.Row]) -> list[dict[str, Any]]:
    """Ask the model to review rule proposals, never individual source rows."""
    review_items = []
    for row in proposals:
        review_items.append(
            {
                "proposalId": row["proposal_id"],
                "ruleKey": row["rule_key"],
                "title": row["title"],
                "objective": row["objective"],
                "operation": row["operation"],
                "condition": json.loads(row["condition_json"] or "{}"),
                "parameters": json.loads(row["parameters_json"] or "{}"),
                "scope": json.loads(row["scope_json"] or "{}"),
                "evidence": json.loads(row["evidence_json"] or "{}"),
                "examples": json.loads(row["examples_json"] or "[]")[:5],
                "expectedCount": int(row["expected_count"] or 0),
                "confidence": float(row["confidence"] or 0),
                "riskLevel": row["risk_level"],
            }
        )
    payload = {
        "task": "审核设备描述统一语义规则草案，只审核规则，不审核或改写单条设备数据",
        "outputSchema": {
            "reviews": [
                {
                    "proposalId": "",
                    "decision": "accept_rule|needs_review|reject",
                    "confidence": 0.0,
                    "reason": "",
                    "requiredChecks": [],
                }
            ]
        },
        "policy": [
            "只允许可解释的确定性规则",
            "不得补造制造商、型号、容量或设备类型",
            "低风险、证据充分、可回放的规则才可建议通过",
            "复杂语义、分类冲突、位置冲突和证据不足必须需要复核",
        ],
        "proposals": review_items,
    }
    try:
        text = invoke_rule_agent_completion(
            [
                {"role": "system", "content": "你是电厂设备语义规则审核智能体。只输出合法 JSON，不输出 Markdown 或解释文字。"},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            "规则审核智能体",
        )
        parsed = parse_rule_agent_json(text, "规则审核智能体")
    except RuleAgentCallError as exc:
        status = 503 if exc.code == "agent_not_configured" else 502
        raise HTTPException(status_code=status, detail=rule_agent_failure_detail(exc)) from exc
    reviews = parsed.get("reviews", []) if isinstance(parsed, dict) else parsed
    return [item for item in reviews if isinstance(item, dict)] if isinstance(reviews, list) else []


def deterministic_rule_review_gate(row: sqlite3.Row, model_review: dict[str, Any]) -> tuple[str, float, str]:
    """Constrain an AI recommendation before it is shown as auto-acceptable."""
    raw_decision = str(model_review.get("decision") or "needs_review").strip().lower()
    confidence = max(0.0, min(1.0, float(model_review.get("confidence") or 0)))
    reason = str(model_review.get("reason") or "智能体未提供充分理由").strip()[:1000]
    if raw_decision in {"reject", "rejected"}:
        return "reject", confidence, reason
    checks: list[str] = []
    operation = str(row["operation"] or "").strip().lower()
    if operation not in {"replace", "normalize", "trim"}:
        checks.append("仅允许 replace/normalize/trim 自动处理")
    if row["risk_level"] != "low":
        checks.append("风险等级不是 low")
    if float(row["confidence"] or 0) < 0.85:
        checks.append("规则草案置信度低于 0.85")
    if int(row["expected_count"] or 0) <= 0:
        checks.append("预计影响数量为 0")
    examples = json.loads(row["examples_json"] or "[]")
    if not isinstance(examples, list) or len(examples) < 2:
        checks.append("有效前后样本少于 2 条")
    if operation == "replace":
        parameters = json.loads(row["parameters_json"] or "{}")
        if not str(parameters.get("from") or parameters.get("source") or "").strip():
            checks.append("replace 缺少明确的 from/source")
    if raw_decision not in {"accept_rule", "accept", "approve", "approved"}:
        checks.append("智能体建议需要复核")
    if confidence < 0.85:
        checks.append("智能体审核置信度低于 0.85")
    if checks:
        return "needs_review", confidence, f"{reason}；" + "；".join(checks)
    return "accept_rule", confidence, reason


def auto_process_accepted_rule_agent_proposal(
    connection: sqlite3.Connection,
    proposal: sqlite3.Row,
) -> sqlite3.Row:
    """Replay first; write a preview only after every active case passes."""
    target_rows = rule_agent_target_rows(connection, proposal)
    if not target_rows:
        return proposal
    replay_id = f"replay-agent-eval-{proposal['proposal_id']}-{hashlib.sha256(proposal['rule_version'].encode()).hexdigest()[:12]}"
    evaluation = replay_rule_agent_against_evaluation_cases(connection, proposal, replay_id)
    now = utc_now()
    status = "replayed" if evaluation["status"] == "passed" else "failed"
    preview_path: Path | None = None
    sample_path: Path | None = None
    preview_sha: str | None = None
    if status == "replayed":
        preview_path, sample_path, preview_sha = rule_agent_write_preview(proposal, target_rows)
    connection.execute(
        """
        UPDATE rule_agent_proposal
        SET status=?,preview_path=?,sample_path=?,preview_sha256=?,preview_count=?,
            replay_count=?,replay_pass_count=?,replay_fail_count=?,
            evaluation_replay_id=?,evaluation_count=?,evaluation_pass_count=?,evaluation_fail_count=?,
            replay_message=?,updated_at=?
        WHERE proposal_id=?
        """,
        (
            status,
            str(preview_path) if preview_path else None,
            str(sample_path) if sample_path else None,
            preview_sha,
            len(target_rows) if status == "replayed" else 0,
            len(target_rows) if status == "replayed" else 0,
            len(target_rows) if status == "replayed" else 0,
            0 if status == "replayed" else len(target_rows),
            evaluation["replayId"],
            evaluation["evaluationCount"],
            evaluation["passCount"],
            evaluation["failCount"],
            None if status == "replayed" else json.dumps(evaluation["failures"][:50], ensure_ascii=False),
            now,
            proposal["proposal_id"],
        ),
    )
    connection.execute(
        "INSERT INTO audit_event(entity_type,entity_id,event_type,actor,payload_json,event_at) VALUES (?,?,?,?,?,?)",
        (
            "rule_agent_proposal",
            proposal["proposal_id"],
            "rule_agent_auto_preview_replay_completed",
            "rule-agent-review",
            json.dumps(
                {
                    "proposalId": proposal["proposal_id"],
                    "candidateMatchCount": len(target_rows),
                    "previewCount": len(target_rows) if status == "replayed" else 0,
                    "evaluationReplayId": evaluation["replayId"],
                    "evaluationCount": evaluation["evaluationCount"],
                    "evaluationPassCount": evaluation["passCount"],
                    "evaluationFailCount": evaluation["failCount"],
                    "status": status,
                    "sourceWrite": False,
                    "formalPublication": False,
                },
                ensure_ascii=False,
            ),
            now,
        ),
    )
    return rule_agent_proposal_row(connection, proposal["proposal_id"])


def rule_agent_proposal_payload(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "proposalId": row["proposal_id"],
        "runId": row["run_id"],
        "ruleKey": row["rule_key"],
        "ruleVersion": row["rule_version"],
        "title": row["title"],
        "objective": row["objective"],
        "operation": row["operation"],
        "condition": json.loads(row["condition_json"] or "{}"),
        "parameters": json.loads(row["parameters_json"] or "{}"),
        "scope": json.loads(row["scope_json"] or "{}"),
        "evidence": json.loads(row["evidence_json"] or "{}"),
        "examples": json.loads(row["examples_json"] or "[]"),
        "expectedCount": int(row["expected_count"]),
        "confidence": float(row["confidence"]),
        "riskLevel": row["risk_level"],
        "status": row["status"],
        "discoveryFilterStatus": row["discovery_filter_status"],
        "discoveryFilterReason": row["discovery_filter_reason"],
        "discoveryFilteredAt": row["discovery_filtered_at"],
        "previewPath": row["preview_path"],
        "samplePath": row["sample_path"],
        "previewCount": int(row["preview_count"]),
        "replayCount": int(row["replay_count"]),
        "replayPassCount": int(row["replay_pass_count"]),
        "replayFailCount": int(row["replay_fail_count"]),
        "evaluationReplayId": row["evaluation_replay_id"],
        "evaluationCount": int(row["evaluation_count"] or 0),
        "evaluationPassCount": int(row["evaluation_pass_count"] or 0),
        "evaluationFailCount": int(row["evaluation_fail_count"] or 0),
        "agentReviewDecision": row["agent_review_decision"],
        "agentReviewConfidence": float(row["agent_review_confidence"] or 0),
        "agentReviewReason": row["agent_review_reason"],
        "agentReviewVersion": row["agent_review_version"],
        "agentReviewedAt": row["agent_reviewed_at"],
        "replayMessage": row["replay_message"],
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }


def rule_agent_scope_matches(row: sqlite3.Row, scope: dict[str, Any]) -> bool:
    """Apply explicit site/classification/KKS/location scope."""
    sites = scope.get("sites") or scope.get("siteIds") or []
    classifications = scope.get("classifications") or scope.get("classificationIds") or []
    location_parents = scope.get("locationParents") or scope.get("locationParent") or []
    location_codes = scope.get("locationCodes") or scope.get("locationCode") or []
    kks_prefixes = scope.get("kksPrefixes") or scope.get("kksPrefix") or []
    if isinstance(sites, str):
        sites = [sites]
    if isinstance(classifications, str):
        classifications = [classifications]
    if isinstance(location_parents, str):
        location_parents = [location_parents]
    if isinstance(location_codes, str):
        location_codes = [location_codes]
    if isinstance(kks_prefixes, str):
        kks_prefixes = [kks_prefixes]
    site_values = {str(value).strip() for value in sites if str(value).strip()}
    class_values = {str(value).strip() for value in classifications if str(value).strip()}
    parent_values = {str(value).strip() for value in location_parents if str(value).strip()}
    code_values = {str(value).strip() for value in location_codes if str(value).strip()}
    prefix_values = {str(value).strip() for value in kks_prefixes if str(value).strip()}
    location_parent = row["location_parent"] or ""
    location_code = row["location_code"] or ""
    return (
        (not site_values or (row["site_id"] or "") in site_values)
        and (not class_values or (row["classification_description"] or "") in class_values)
        and (not parent_values or location_parent in parent_values)
        and (not code_values or location_code in code_values)
        and (not prefix_values or any(location_code.startswith(prefix) for prefix in prefix_values))
    )


def rule_agent_transform(original: str, operation: str, condition: dict[str, Any], parameters: dict[str, Any]) -> tuple[bool, str, str]:
    """Execute the deliberately small, auditable rule vocabulary used by agent proposals."""
    text = original or ""
    contains = condition.get("contains") or condition.get("descriptionContains")
    pattern = condition.get("descriptionPattern")
    prefix = condition.get("prefix")
    suffix = condition.get("suffix")
    if pattern is not None and contains is None and prefix is None and suffix is None:
        return False, text, "unsupported_description_pattern"
    if contains is not None and str(contains) not in text:
        return False, text, "condition_not_matched"
    if prefix is not None and not text.startswith(str(prefix)):
        return False, text, "prefix_not_matched"
    if suffix is not None and not text.endswith(str(suffix)):
        return False, text, "suffix_not_matched"
    normalized_operation = operation.strip().lower()
    if normalized_operation == "replace":
        source = str(parameters.get("from") or parameters.get("source") or "")
        target = str(parameters.get("to") if parameters.get("to") is not None else parameters.get("target") or "")
        if not source:
            raise HTTPException(status_code=409, detail="规则草案缺少 replace.from，不能安全执行")
        if source not in text:
            return False, text, "source_not_matched"
        return True, text.replace(source, target), "replace"
    if normalized_operation == "trim":
        transformed = text.strip()
        return transformed != text, transformed, "trim"
    if normalized_operation == "normalize":
        mode = str(parameters.get("mode") or "legacy").strip().lower()
        if mode == "nfkc":
            transformed = unicodedata.normalize("NFKC", text)
        elif mode == "whitespace":
            transformed = re.sub(r"[\s\u3000]+", " ", text).strip()
        elif mode in {"legacy", "nfkc_whitespace"}:
            transformed = unicodedata.normalize("NFKC", text)
            if parameters.get("whitespace", True):
                transformed = re.sub(r"[\s\u3000]+", " ", transformed).strip()
        else:
            raise HTTPException(status_code=409, detail=f"normalize mode={mode} is not supported")
        return transformed != text, transformed, "normalize"
    if normalized_operation in {"keep_original", "block"}:
        return True, text, normalized_operation
    raise HTTPException(status_code=409, detail=f"规则草案 operation={operation} 不在安全执行器白名单内")


def rule_agent_target_rows(connection: sqlite3.Connection, proposal: sqlite3.Row | dict[str, Any]) -> list[dict[str, Any]]:
    batch = connection.execute("SELECT batch_id FROM batch_run WHERE batch_id=(SELECT batch_id FROM rule_agent_run WHERE run_id=?)", (proposal["run_id"],)).fetchone()
    if batch is None:
        raise HTTPException(status_code=409, detail="规则草案关联的批次不存在")
    rows = connection.execute(
        f"""
        SELECT c.candidate_id,c.original_description,c.candidate_description,
          d.site_id,d.asset_number,d.location_code,d.location_description,
          d.location_parent,d.classification_description
        FROM semantic_candidate c JOIN device_identity d ON d.device_id=c.device_id
        WHERE c.batch_id=? AND c.confidence='high' AND {RULE_AGENT_EVIDENCE_SQL}
          AND c.validator_status='candidate'
          AND c.review_state='pending' AND c.publication_state='unpublished'
          AND length(trim(c.original_description)) > 0
        ORDER BY d.site_id,COALESCE(d.classification_description,''),c.candidate_id
        """,
        (batch["batch_id"],),
    ).fetchall()
    condition = json.loads(proposal["condition_json"] or "{}")
    parameters = json.loads(proposal["parameters_json"] or "{}")
    scope = json.loads(proposal["scope_json"] or "{}")
    target_rows: list[dict[str, Any]] = []
    for row in rows:
        if not rule_agent_scope_matches(row, scope):
            continue
        matched, transformed, reason = rule_agent_transform(row["original_description"] or "", proposal["operation"], condition, parameters)
        if not matched:
            continue
        target_rows.append({
            "CANDIDATE_ID": row["candidate_id"],
            "SITEID": row["site_id"] or "",
            "ASSETNUM": row["asset_number"] or "",
            "ORIGINAL_DESCRIPTION": row["original_description"] or "",
            "PROPOSED_DESCRIPTION": transformed,
            "LOCATION": row["location_code"] or "",
            "LOCATION_DESCRIPTION": row["location_description"] or "",
            "LOCATION_PARENT": row["location_parent"] or "",
            "CLASSIFICATION": row["classification_description"] or "",
            "RULE_KEY": proposal["rule_key"],
            "OPERATION": proposal["operation"],
            "MATCH_REASON": reason,
        })
    return target_rows


def measure_rule_agent_proposal(
    connection: sqlite3.Connection,
    run_id: str,
    rule_key: str,
    operation: str,
    condition: dict[str, Any],
    parameters: dict[str, Any],
    scope: dict[str, Any],
) -> dict[str, Any]:
    """Measure a model proposal against local candidates before persisting it."""
    proposal_ref = {
        "run_id": run_id,
        "rule_key": rule_key,
        "operation": operation,
        "condition_json": json.dumps(condition, ensure_ascii=False),
        "parameters_json": json.dumps(parameters, ensure_ascii=False),
        "scope_json": json.dumps(scope, ensure_ascii=False),
    }
    try:
        target_rows = rule_agent_target_rows(connection, proposal_ref)
    except HTTPException as exc:
        return {
            "matchedCount": 0,
            "sampleCount": 0,
            "examples": [],
            "eligible": False,
            "filterReason": f"local_executor_error:{str(exc.detail)[:200]}",
        }

    examples = [
        {
            "before": row["ORIGINAL_DESCRIPTION"],
            "after": row["PROPOSED_DESCRIPTION"],
            "candidateId": row["CANDIDATE_ID"],
            "siteId": row["SITEID"],
        }
        for row in target_rows[:5]
    ]
    matched_count = len(target_rows)
    sample_count = len(examples)
    eligible = matched_count >= RULE_AGENT_MIN_MATCH_COUNT and sample_count >= RULE_AGENT_MIN_EVIDENCE_SAMPLES
    return {
        "matchedCount": matched_count,
        "sampleCount": sample_count,
        "examples": examples,
        "eligible": eligible,
        "filterReason": "local_match_and_sample_evidence" if eligible else "no_local_match_or_sample_evidence",
    }


def enrich_rule_agent_proposal_from_local_preview(
    connection: sqlite3.Connection,
    proposal: sqlite3.Row,
) -> sqlite3.Row:
    """Replace model-estimated impact with measured local preview evidence."""
    try:
        target_rows = rule_agent_target_rows(connection, proposal)
    except HTTPException:
        return proposal
    examples = json.loads(proposal["examples_json"] or "[]")
    if not isinstance(examples, list) or len(examples) < 2:
        examples = [
            {
                "before": item["ORIGINAL_DESCRIPTION"],
                "after": item["PROPOSED_DESCRIPTION"],
                "candidateId": item["CANDIDATE_ID"],
            }
            for item in target_rows[:5]
        ]
    evidence = json.loads(proposal["evidence_json"] or "{}")
    if not isinstance(evidence, dict):
        evidence = {}
    evidence.update({"matchedCount": len(target_rows), "sampleCount": min(200, len(target_rows)), "measuredFromLocalPreview": True})
    connection.execute(
        """
        UPDATE rule_agent_proposal
        SET expected_count=?,examples_json=?,evidence_json=?,updated_at=?
        WHERE proposal_id=?
        """,
        (len(target_rows), json.dumps(examples, ensure_ascii=False), json.dumps(evidence, ensure_ascii=False), utc_now(), proposal["proposal_id"]),
    )
    return rule_agent_proposal_row(connection, proposal["proposal_id"])


def rule_agent_stratified_sample(rows: list[dict[str, Any]], sample_size: int = 200) -> list[dict[str, Any]]:
    """Select a deterministic, proportional sample across sites for review."""
    if len(rows) <= sample_size:
        return list(rows)

    grouped: dict[str, list[tuple[int, dict[str, Any]]]] = {}
    for index, row in enumerate(rows):
        site = str(row.get("SITEID") or "").strip() or "__UNKNOWN__"
        grouped.setdefault(site, []).append((index, row))

    sites = sorted(grouped)
    if len(sites) > sample_size:
        return list(rows[:sample_size])

    population = len(rows)
    raw_quota = {site: sample_size * len(grouped[site]) / population for site in sites}
    quota = {
        site: min(len(grouped[site]), max(1, int(raw_quota[site])))
        for site in sites
    }

    while sum(quota.values()) < sample_size:
        candidates = [site for site in sites if quota[site] < len(grouped[site])]
        if not candidates:
            break
        site = max(candidates, key=lambda value: (raw_quota[value] - quota[value], -sites.index(value)))
        quota[site] += 1

    while sum(quota.values()) > sample_size:
        candidates = [site for site in sites if quota[site] > 1]
        if not candidates:
            break
        site = max(candidates, key=lambda value: (quota[value] - raw_quota[value], -sites.index(value)))
        quota[site] -= 1

    selected: list[tuple[int, dict[str, Any]]] = []
    for site in sites:
        group = grouped[site]
        target = quota[site]
        if target >= len(group):
            selected.extend(group)
            continue
        if target == 1:
            selected.append(group[len(group) // 2])
            continue
        indices = [round(index * (len(group) - 1) / (target - 1)) for index in range(target)]
        selected.extend(group[index] for index in indices)

    selected.sort(key=lambda item: item[0])
    return [row for _, row in selected[:sample_size]]


def rule_agent_write_preview(proposal: sqlite3.Row, rows: list[dict[str, Any]]) -> tuple[Path, Path, str]:
    directory = RULE_AGENT_DIR / proposal["proposal_id"]
    directory.mkdir(parents=True, exist_ok=True)
    preview_path = directory / "preview.csv"
    sample_path = directory / "sample_200.csv"
    fieldnames = ["CANDIDATE_ID", "SITEID", "ASSETNUM", "ORIGINAL_DESCRIPTION", "PROPOSED_DESCRIPTION", "LOCATION", "LOCATION_DESCRIPTION", "LOCATION_PARENT", "CLASSIFICATION", "RULE_KEY", "OPERATION", "MATCH_REASON"]
    sample_rows = rule_agent_stratified_sample(rows, sample_size=200)
    for path, values in ((preview_path, rows), (sample_path, sample_rows)):
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(values)
    return preview_path, sample_path, sha256_file(preview_path)


def rule_agent_case_scope_matches(case: sqlite3.Row, scope: dict[str, Any]) -> bool:
    """Match an evaluation case using only persisted case context."""
    context: dict[str, Any] = {}
    try:
        parsed = json.loads(case["context_json"] or "{}")
        if isinstance(parsed, dict):
            context = parsed
    except json.JSONDecodeError:
        context = {}
    row = {
        "site_id": case["site_id"] or "",
        "classification_description": context.get("classificationDescription")
        or context.get("classification")
        or context.get("classification_description")
        or "",
        "location_parent": context.get("locationParent") or context.get("location_parent") or "",
        "location_code": context.get("locationCode") or context.get("location_code") or context.get("kks") or "",
    }
    return rule_agent_scope_matches(row, scope)


def evaluate_rule_agent_catalog_spec(
    connection: sqlite3.Connection,
    spec: dict[str, Any],
) -> dict[str, Any]:
    """Reject a mined pattern before AI review if it regresses active cases."""
    cases = connection.execute("SELECT * FROM evaluation_case WHERE active=1 ORDER BY case_id").fetchall()
    failures: list[dict[str, Any]] = []
    pass_count = 0
    for case in cases:
        if not rule_agent_case_scope_matches(case, spec.get("scope") or {}):
            pass_count += 1
            continue
        matched, transformed, reason = rule_agent_transform(
            case["input_description"],
            spec["operation"],
            spec.get("condition") or {},
            spec.get("parameters") or {},
        )
        message: dict[str, Any] | None = None
        if matched and spec["operation"] in {"keep_original", "block"}:
            message = {"reason": "non_transforming_operation_cannot_change_evaluation_case"}
        elif matched and (
            case["expected_decision"] not in {"approved", "modified"}
            or transformed != case["expected_description"]
        ):
            message = {
                "failureType": case["failure_type"],
                "reason": reason,
                "expectedDecision": case["expected_decision"],
                "expectedDescription": case["expected_description"],
                "actualDescription": transformed,
            }
        if message:
            if len(failures) < 20:
                failures.append({"caseId": case["case_id"], "reason": message})
        else:
            pass_count += 1
    return {
        "evaluationCount": len(cases),
        "evaluationPassCount": pass_count,
        "evaluationFailCount": len(failures) if len(failures) < 20 else len(cases) - pass_count,
        "status": "passed" if not failures else "failed",
        "failures": failures,
    }


def replay_rule_agent_against_evaluation_cases(
    connection: sqlite3.Connection,
    proposal: sqlite3.Row,
    replay_id: str,
) -> dict[str, Any]:
    """Replay a proposed rule against every active historical evaluation case."""
    existing = connection.execute(
        "SELECT replay_id,status,evaluation_count,pass_count,fail_count FROM replay_run WHERE replay_id=?",
        (replay_id,),
    ).fetchone()
    if existing is not None:
        return {
            "replayId": existing["replay_id"],
            "status": existing["status"],
            "evaluationCount": int(existing["evaluation_count"]),
            "passCount": int(existing["pass_count"]),
            "failCount": int(existing["fail_count"]),
            "failures": [],
        }

    cases = connection.execute(
        "SELECT * FROM evaluation_case WHERE active=1 ORDER BY case_id"
    ).fetchall()
    operation = str(proposal["operation"] or "").strip().lower()
    condition = json.loads(proposal["condition_json"] or "{}")
    parameters = json.loads(proposal["parameters_json"] or "{}")
    scope = json.loads(proposal["scope_json"] or "{}")
    started_at = utc_now()
    pass_count = 0
    fail_count = 0
    failures: list[dict[str, str]] = []
    results: list[tuple[str, str, str | None, str, str | None]] = []

    for case in cases:
        expected_decision = case["expected_decision"]
        expected_description = case["expected_description"]
        matched = False
        actual_decision = expected_decision
        actual_description = expected_description
        message: str | None = None
        if rule_agent_case_scope_matches(case, scope):
            matched, transformed, reason = rule_agent_transform(
                case["input_description"], operation, condition, parameters
            )
            if matched and operation not in {"keep_original", "block"}:
                actual_decision = "modified" if transformed != case["input_description"] else "approved"
                actual_description = transformed
                if expected_decision not in {"approved", "modified"} or actual_description != expected_description:
                    message = json.dumps(
                        {
                            "failureType": case["failure_type"],
                            "reason": reason,
                            "expectedDecision": expected_decision,
                            "actualDecision": actual_decision,
                            "expectedDescription": expected_description,
                            "actualDescription": actual_description,
                        },
                        ensure_ascii=False,
                    )
            elif matched and operation in {"keep_original", "block"}:
                message = json.dumps(
                    {"reason": "non_transforming_operation_cannot_change_evaluation_case", "operation": operation},
                    ensure_ascii=False,
                )
        outcome = "fail" if message else "pass"
        if outcome == "pass":
            pass_count += 1
        else:
            fail_count += 1
            if len(failures) < 200:
                failures.append({"caseId": case["case_id"], "reason": message or "evaluation_regression"})
        results.append((case["case_id"], actual_decision, actual_description, outcome, message))

    finished_at = utc_now()
    connection.execute(
        """
        INSERT INTO replay_run
          (replay_id,rule_version,validator_version,evaluation_count,pass_count,fail_count,status,started_at,finished_at)
        VALUES (?,?,?,?,?,?,?,?,?)
        """,
        (
            replay_id,
            proposal["rule_version"],
            "hd-semantic-validator-0.3.0",
            len(cases),
            pass_count,
            fail_count,
            "passed" if fail_count == 0 else "failed",
            started_at,
            finished_at,
        ),
    )
    connection.executemany(
        "INSERT INTO replay_result(replay_id,case_id,actual_decision,actual_description,outcome,message) VALUES (?,?,?,?,?,?)",
        [(replay_id, *result) for result in results],
    )
    connection.execute(
        """
        INSERT INTO audit_event(entity_type,entity_id,event_type,actor,payload_json,event_at)
        VALUES (?,?,?,?,?,?)
        """,
        (
            "rule_agent_proposal",
            proposal["proposal_id"],
            "rule_agent_evaluation_replay_completed",
            "rule-agent-gate",
            json.dumps(
                {
                    "proposalId": proposal["proposal_id"],
                    "replayId": replay_id,
                    "evaluationCount": len(cases),
                    "passCount": pass_count,
                    "failCount": fail_count,
                    "sourceWrite": False,
                    "formalPublication": False,
                },
                ensure_ascii=False,
            ),
            finished_at,
        ),
    )
    return {
        "replayId": replay_id,
        "status": "passed" if fail_count == 0 else "failed",
        "evaluationCount": len(cases),
        "passCount": pass_count,
        "failCount": fail_count,
        "failures": failures,
    }


def rule_agent_proposal_row(connection: sqlite3.Connection, proposal_id: str) -> sqlite3.Row:
    row = connection.execute("SELECT * FROM rule_agent_proposal WHERE proposal_id=?", (proposal_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"规则草案不存在：{proposal_id}")
    if row["discovery_filter_status"] != "eligible":
        raise HTTPException(status_code=409, detail="规则草案已被本地证据门禁隔离，不能继续预览、回放或启用")
    return row


DEFAULT_SAMPLE_TARGET = 300
MAX_SAMPLE_TARGET = 5000
AI_SAMPLE_ID = "next-ai-review-20260812T085842Z"
AI_DECISION_KEYS = ("keep_original", "accept_candidate", "needs_review")


def load_ai_review_sample() -> tuple[str, list[dict[str, str]]]:
    if not AI_REVIEW_SAMPLE_CSV.exists():
        raise HTTPException(status_code=503, detail="AI 语义样本尚未生成")
    sample_id = AI_SAMPLE_ID
    if AI_REVIEW_MANIFEST.exists():
        try:
            sample_id = str(json.loads(AI_REVIEW_MANIFEST.read_text(encoding="utf-8")).get("sample_id") or AI_SAMPLE_ID)
        except (OSError, json.JSONDecodeError):
            pass
    with AI_REVIEW_SAMPLE_CSV.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    return sample_id, rows


AI_CLUSTER_TYPES = ("question_context", "leading_minus", "terminal_hyphen", "other")


def cluster_pattern(kind: str, original: str, candidate: str) -> str:
    """Return a stable, explainable rule signature for the changed description."""
    value = original
    if kind == "leading_minus" and value.startswith("-"):
        value = value[1:]
    if kind == "terminal_hyphen" and value.endswith("-"):
        value = value[:-1]
    value = value.replace("？", "?")
    value = re.sub(r"\d+(?:\.\d+)?", "{N}", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value or "<empty>"


def classify_ai_cluster(original: str, candidate: str) -> tuple[str, str, str, float, str]:
    """Classify only the current 542 changed high-quality rows.

    The classifier is deliberately conservative: a cluster decision is a local
    review record and never changes semantic_candidate.review_state.
    """
    if original.replace("？", "?") == candidate and original != candidate:
        return (
            "question_context",
            "问号上下文",
            "needs_review",
            0.88,
            "问号可能表达相位、占位或未知字段，先按簇确认，不自动删除。",
        )
    if original.startswith("-") and original[1:] == candidate:
        return (
            "leading_minus",
            "前导负号",
            "keep_original",
            0.99,
            "删除前导负号可能改变负标高、负电压或负值含义，默认保留原文。",
        )
    if original.endswith("-") and original[:-1] == candidate:
        return (
            "terminal_hyphen",
            "末尾横线",
            "needs_review",
            0.83,
            "末尾横线可能是孤立标记，也可能属于设备命名习惯，需按簇确认。",
        )
    return (
        "other",
        "其他差异",
        "needs_review",
        0.60,
        "差异未命中当前确定性规则，保留人工复核入口。",
    )


def ai_cluster_id(kind: str, pattern: str) -> str:
    digest = hashlib.sha256(f"remaining-diff-v1|{kind}|{pattern}".encode("utf-8")).hexdigest()[:20]
    return f"cluster-{digest}"


AI_CLUSTER_CACHE_TTL_SECONDS = 300
_AI_CLUSTER_ROWS_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_AI_CLUSTER_SUMMARY_CACHE: dict[str, tuple[float, list[dict[str, Any]], dict[str, Any]]] = {}


def invalidate_ai_cluster_cache() -> None:
    _AI_CLUSTER_ROWS_CACHE.clear()
    _AI_CLUSTER_SUMMARY_CACHE.clear()


def load_ai_cluster_rows(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    """Load the unpublished changed high-quality scope without touching source tables."""
    batch = latest_batch(connection)
    batch_id = str(batch["batch_id"])

    def attach_decisions(base_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        decisions = {
            row["candidate_id"]: row["decision"]
            for row in connection.execute("SELECT candidate_id,decision FROM ai_review_decision").fetchall()
        }
        cluster_decisions = {
            row["cluster_id"]: dict(row)
            for row in connection.execute("SELECT * FROM ai_cluster_decision").fetchall()
        }
        return [
            {
                **row,
                "memberDecision": decisions.get(row["candidateId"]),
                "clusterDecision": cluster_decisions.get(row["clusterId"], {}).get("decision"),
            }
            for row in base_rows
        ]

    cached = _AI_CLUSTER_ROWS_CACHE.get(batch_id)
    if cached and time.monotonic() - cached[0] < AI_CLUSTER_CACHE_TTL_SECONDS:
        return cached[1]

    rows = connection.execute(
        f"""
        SELECT c.candidate_id,c.batch_id,c.original_description,c.candidate_description,
          c.confidence,c.validator_status,c.review_state,c.publication_state,
          c.reason_codes_json,c.evidence_level,c.applied_rule_ids_json,c.rule_version,
          c.validator_version,c.created_at,d.source_asset_id,d.site_id,d.asset_number,
          d.location_code,d.location_description,d.location_parent,
          d.classification_description
        FROM semantic_candidate c
        JOIN device_identity d ON d.device_id=c.device_id
        WHERE c.batch_id=?
          AND c.review_state='pending'
          AND c.validator_status='candidate'
          AND c.confidence='high'
          AND {RULE_AGENT_EVIDENCE_SQL}
          AND c.publication_state='unpublished'
          AND length(trim(c.original_description)) > 0
          AND length(trim(c.candidate_description)) > 0
          AND c.original_description <> c.candidate_description
        ORDER BY d.site_id,d.asset_number,c.candidate_id
        """,
        (batch_id,),
    ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        original = str(row["original_description"] or "")
        candidate = str(row["candidate_description"] or "")
        kind, label, recommendation, ai_confidence, reason = classify_ai_cluster(original, candidate)
        pattern = cluster_pattern(kind, original, candidate)
        cluster_id = ai_cluster_id(kind, pattern)
        result.append({
            "clusterId": cluster_id,
            "clusterType": kind,
            "clusterLabel": label,
            "ruleSignature": f"{kind}:{pattern}",
            "clusterPattern": pattern,
            "aiDecision": recommendation,
            "aiConfidence": ai_confidence,
            "aiReason": reason,
            "candidateId": row["candidate_id"],
            "batchId": row["batch_id"],
            "siteId": row["site_id"],
            "assetNumber": row["asset_number"],
            "originalDescription": original,
            "candidateDescription": candidate,
            "kks": row["location_code"] or "",
            "locationDescription": row["location_description"] or "",
            "locationParent": row["location_parent"] or "",
            "classificationDescription": row["classification_description"] or "",
            "confidence": row["confidence"],
            "reviewState": row["review_state"],
            "reasonCodes": parse_json_array(row["reason_codes_json"]),
            "appliedRules": parse_json_array(row["applied_rule_ids_json"]),
            "ruleVersion": row["rule_version"],
            "validatorVersion": row["validator_version"],
            "createdAt": row["created_at"],
            "memberDecision": None,
            "clusterDecision": None,
        })
    enriched = attach_decisions(result)
    _AI_CLUSTER_ROWS_CACHE[batch_id] = (time.monotonic(), enriched)
    return enriched


def summarize_ai_clusters(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(row["clusterId"], []).append(row)
    clusters: list[dict[str, Any]] = []
    suggestion_counts = {key: 0 for key in AI_DECISION_KEYS}
    decision_counts = {key: 0 for key in AI_DECISION_KEYS}
    reviewed_candidates = 0
    site_counts = Counter((row.get("siteId") or "").strip() or "未填写" for row in rows)
    classification_counts = Counter((row.get("classificationDescription") or "").strip() or "未分类" for row in rows)
    for cluster_id, members in grouped.items():
        members.sort(key=lambda item: (item["siteId"], item["assetNumber"], item["candidateId"]))
        first = members[0]
        # A sample-row decision is evidence on a member, not an implicit
        # approval of the whole cluster. Only the explicit cluster table entry
        # can move a cluster out of the pending state.
        cluster_decision = first["clusterDecision"]
        reviewed_count = sum(1 for item in members if item["memberDecision"])
        reviewed_candidates += reviewed_count
        suggestion_counts[first["aiDecision"]] += 1
        if cluster_decision in decision_counts:
            decision_counts[cluster_decision] += 1
        sites: dict[str, int] = {}
        for item in members:
            sites[item["siteId"]] = sites.get(item["siteId"], 0) + 1
        clusters.append({
            "clusterId": cluster_id,
            "clusterType": first["clusterType"],
            "clusterLabel": first["clusterLabel"],
            "clusterPattern": first["clusterPattern"],
            "ruleSignature": first["ruleSignature"],
            "memberCount": len(members),
            "reviewedCount": reviewed_count,
            "pendingCount": len(members) - reviewed_count,
            "siteIds": sorted(sites),
            "sites": [{"siteId": site_id, "count": count} for site_id, count in sorted(sites.items())],
            "aiDecision": first["aiDecision"],
            "aiConfidence": first["aiConfidence"],
            "aiReason": first["aiReason"],
            "decision": cluster_decision,
            "sample": {key: first[key] for key in ("candidateId", "siteId", "assetNumber", "originalDescription", "candidateDescription", "kks", "locationDescription", "locationParent", "classificationDescription")},
        })
    clusters.sort(key=lambda item: (-item["memberCount"], item["clusterType"], item["clusterId"]))
    summary = {
        "candidateCount": len(rows),
        "clusterCount": len(clusters),
        "pendingCandidateCount": len(rows) - reviewed_candidates,
        "reviewedCandidateCount": reviewed_candidates,
        "pendingClusterCount": sum(1 for item in clusters if item["decision"] is None),
        "aiRecommendation": suggestion_counts,
        "clusterDecision": decision_counts,
        "sites": [{"siteId": key, "count": count} for key, count in sorted(site_counts.items(), key=lambda item: (-item[1], item[0]))],
        "classifications": [{"value": key, "count": count} for key, count in sorted(classification_counts.items(), key=lambda item: (-item[1], item[0]))[:50]],
    }
    return clusters, summary


def ai_sample_summary(connection: sqlite3.Connection, sample_id: str, rows: list[dict[str, str]]) -> dict[str, Any]:
    decisions = {key: 0 for key in AI_DECISION_KEYS}
    for row in connection.execute(
        "SELECT decision, count(*) AS count FROM ai_review_decision WHERE sample_id=? GROUP BY decision",
        (sample_id,),
    ).fetchall():
        if row["decision"] in decisions:
            decisions[row["decision"]] = int(row["count"])
    recommended = {key: 0 for key in AI_DECISION_KEYS}
    for row in rows:
        value = row.get("AI_DECISION", "")
        if value == "保留原文":
            recommended["keep_original"] += 1
        elif value == "接受候选":
            recommended["accept_candidate"] += 1
        elif value == "需要复核":
            recommended["needs_review"] += 1
    reviewed_ids = {row["candidate_id"] for row in connection.execute("SELECT candidate_id FROM ai_review_decision WHERE sample_id=?", (sample_id,)).fetchall()}
    pending_recommendation = {key: 0 for key in AI_DECISION_KEYS}
    for row in rows:
        if row.get("CANDIDATE_ID") in reviewed_ids:
            continue
        value = row.get("AI_DECISION", "")
        if value == "保留原文":
            pending_recommendation["keep_original"] += 1
        elif value == "接受候选":
            pending_recommendation["accept_candidate"] += 1
        elif value == "需要复核":
            pending_recommendation["needs_review"] += 1
    site_counts = Counter((row.get("SITEID") or "").strip() or "未填写" for row in rows)
    classification_counts = Counter((row.get("CLASSIFICATION_DESCRIPTION") or "").strip() or "未分类" for row in rows)
    return {
        "sampleId": sample_id,
        "total": len(rows),
        "reviewed": sum(decisions.values()),
        "pending": len(rows) - sum(decisions.values()),
        "reviewedByDecision": decisions,
        "aiRecommendation": recommended,
        "pendingByRecommendation": pending_recommendation,
        "sites": [{"siteId": key, "count": count} for key, count in sorted(site_counts.items(), key=lambda item: (-item[1], item[0]))],
        "classifications": [{"value": key, "count": count} for key, count in sorted(classification_counts.items(), key=lambda item: (-item[1], item[0]))[:50]],
    }


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


def row_to_publication(row: sqlite3.Row) -> dict[str, Any]:
    """Map the formal-result row without exposing mutable source-table state."""
    applied_rules = parse_json_array(row["applied_rule_ids_json"])
    original = row["original_description"] or ""
    final = row["final_description"] or ""
    if "format.fullwidth_parenthesis_to_ascii" not in applied_rules and final != original and any(mark in original for mark in ("（", "）")):
        applied_rules.append("format.fullwidth_parenthesis_to_ascii")
    if "format.fullwidth_comma_to_ascii" not in applied_rules and final != original and "，" in original:
        applied_rules.append("format.fullwidth_comma_to_ascii")
    return {
        "publicationId": row["publication_id"],
        "candidateId": row["candidate_id"],
        "reviewId": row["review_id"],
        "batchId": row["batch_id"],
        "sourceSnapshotId": row["source_snapshot_id"],
        "siteId": row["site_id"],
        "assetNumber": row["asset_number"],
        "assetId": row["source_asset_id"] or "",
        "originalDescription": row["original_description"] or "",
        "finalDescription": row["final_description"],
        "kks": row["location_code"] or "",
        "locationDescription": row["location_description"] or "",
        "locationParent": row["location_parent"] or "",
        "classificationDescription": row["classification_description"] or "",
        "appliedRules": applied_rules,
        "ruleVersion": row["rule_version"],
        "validatorVersion": row["validator_version"],
        "replayId": row["replay_id"],
        "publishedBy": row["published_by"],
        "publishedAt": row["published_at"],
        "approvalReceipt": row["approval_receipt"],
        "reviewer": row["reviewer"],
        "reviewedAt": row["reviewed_at"],
    }


PUBLISHED_SELECT = """
    SELECT p.publication_id,p.candidate_id,p.review_id,p.source_snapshot_id,
      p.site_id,p.asset_number,p.final_description,p.rule_version,
      p.validator_version,p.replay_id,p.published_by,p.published_at,
      c.batch_id,c.original_description,c.applied_rule_ids_json,
      d.source_asset_id,d.location_code,d.location_description,d.location_parent,
      d.classification_description,r.approval_receipt,r.reviewer,r.reviewed_at
    FROM published_description p
    JOIN semantic_candidate c ON c.candidate_id=p.candidate_id
    JOIN device_identity d ON d.device_id=c.device_id
    JOIN review_decision r ON r.review_id=p.review_id
"""


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


class FormalBatchApprovalRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    replay_id: str = Field(alias="replayId", min_length=1, max_length=200)
    note: str = Field(default="", max_length=500)
    idempotency_key: str = Field(alias="idempotencyKey", min_length=1, max_length=200)


class CleaningBatchApprovalRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    scope: Literal["cleaning"] = "cleaning"
    task_id: str | None = Field(default=None, alias="taskId", max_length=200)
    note: str = Field(default="", max_length=500)
    idempotency_key: str = Field(alias="idempotencyKey", min_length=1, max_length=200)


class CleaningBatchPublishRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    scope: Literal["cleaning"] = "cleaning"
    task_id: str | None = Field(default=None, alias="taskId", max_length=200)
    note: str = Field(default="", max_length=500)
    idempotency_key: str = Field(alias="idempotencyKey", min_length=1, max_length=200)


class CleaningRuleRegistrationRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    rule_key: str = Field(alias="ruleKey", min_length=3, max_length=200)
    cleaning_type: str = Field(alias="cleaningType", min_length=1, max_length=80)
    rule_label: str = Field(alias="ruleLabel", min_length=1, max_length=200)
    action_label: str = Field(default="清洗", alias="actionLabel", max_length=80)
    is_cleaning: bool = Field(default=True, alias="isCleaning")
    replay_id: str = Field(alias="replayId", min_length=3, max_length=200)
    rule_version: str = Field(alias="ruleVersion", min_length=1, max_length=200)


class CleaningTaskActionRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    note: str = Field(default="", max_length=500)
    idempotency_key: str = Field(alias="idempotencyKey", min_length=1, max_length=200)


class CleaningTaskAdvanceRequest(BaseModel):
    """Advance one cleaning task through its declared workflow."""

    model_config = ConfigDict(populate_by_name=True)

    note: str = Field(default="", max_length=500)
    idempotency_key: str = Field(alias="idempotencyKey", min_length=1, max_length=200)
    confirm_publication: bool = Field(default=False, alias="confirmPublication")


class CleaningTaskPreviewRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    preview_id: str | None = Field(default=None, alias="previewId", max_length=200)
    preview_sha256: str | None = Field(default=None, alias="previewSha256", min_length=8, max_length=128)
    preview_path: str | None = Field(default=None, alias="previewPath", max_length=500)
    sample_path: str | None = Field(default=None, alias="samplePath", max_length=500)
    note: str = Field(default="", max_length=500)


class RuleAgentRunRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    idempotency_key: str = Field(alias="idempotencyKey", min_length=3, max_length=200)
    sample_size: int = Field(default=120, alias="sampleSize", ge=20, le=500)
    note: str = Field(default="", max_length=500)


class SemanticReasoningRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    idempotency_key: str = Field(alias="idempotencyKey", min_length=3, max_length=200)
    sample_size: int = Field(default=200, alias="sampleSize", ge=50, le=500)
    cluster_limit: int = Field(default=12, alias="clusterLimit", ge=1, le=30)
    cluster_keys: list[str] = Field(default_factory=list, alias="clusterKeys", max_length=30)
    note: str = Field(default="", max_length=500)


class RuleAgentProposalActionRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    idempotency_key: str = Field(alias="idempotencyKey", min_length=3, max_length=200)
    note: str = Field(default="", max_length=500)


class RuleAgentReviewRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    idempotency_key: str = Field(alias="idempotencyKey", min_length=3, max_length=200)
    proposal_ids: list[str] = Field(default_factory=list, alias="proposalIds", max_length=20)
    note: str = Field(default="", max_length=500)


class AiDecisionRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    sample_id: str = Field(alias="sampleId", min_length=1)
    candidate_id: str = Field(alias="candidateId", min_length=1)
    decision: Literal["keep_original", "accept_candidate", "needs_review"]
    note: str = ""
    idempotency_key: str = Field(alias="idempotencyKey", min_length=1, max_length=200)


class AiBulkDecisionRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    sample_id: str = Field(alias="sampleId", min_length=1)
    decision: Literal["keep_original", "accept_candidate"]
    idempotency_key: str = Field(alias="idempotencyKey", min_length=1, max_length=200)


class AiClusterDecisionRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    cluster_id: str = Field(alias="clusterId", min_length=1, max_length=100)
    decision: Literal["keep_original", "accept_candidate", "needs_review"]
    note: str = ""
    idempotency_key: str = Field(alias="idempotencyKey", min_length=1, max_length=200)


class AutoApprovalRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    scope: Literal["sample", "batch"] = "sample"
    idempotency_key: str = Field(alias="idempotencyKey", min_length=1, max_length=200)


class AgentCandidateReviewRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    idempotency_key: str = Field(alias="idempotencyKey", min_length=3, max_length=200)
    candidate_ids: list[str] = Field(default_factory=list, alias="candidateIds", max_length=CANDIDATE_AGENT_MAX_BATCH_SIZE)
    batch_size: int = Field(default=CANDIDATE_AGENT_DEFAULT_BATCH_SIZE, alias="batchSize", ge=10, le=CANDIDATE_AGENT_MAX_BATCH_SIZE)
    note: str = Field(default="", max_length=500)


class SemanticExecutionPreviewRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    asset_id: str = Field(alias="assetId", min_length=1, max_length=200)
    asset_version_id: str | None = Field(default=None, alias="assetVersionId", max_length=200)
    target_scope: str = Field(default="local_semantic_layer", alias="targetScope", min_length=1, max_length=200)
    sample_size: int = Field(default=200, alias="sampleSize", ge=1, le=500)
    idempotency_key: str = Field(alias="idempotencyKey", min_length=3, max_length=200)
    note: str = Field(default="", max_length=500)


class SemanticExecutionDispatchRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    action_run_id: str | None = Field(default=None, alias="actionRunId", max_length=200)
    action_plan_id: str | None = Field(default=None, alias="actionPlanId", max_length=200)
    approval_receipt: str | None = Field(default=None, alias="approvalReceipt", max_length=300)
    adapter_id: str = Field(default="local-preview-adapter", alias="adapterId", max_length=200)
    idempotency_key: str = Field(alias="idempotencyKey", min_length=3, max_length=200)
    request_payload: dict[str, Any] = Field(default_factory=dict, alias="requestPayload")


class SemanticActionApprovalRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    decision: Literal["approved", "rejected"]
    reviewer: str = Field(default="人工审核", max_length=100)
    comment: str = Field(default="", max_length=500)


class DefectStatusReviewRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    decision: Literal["approved", "rejected"]
    canonical_state: str | None = Field(default=None, alias="canonicalState", max_length=40)
    business_meaning: str | None = Field(default=None, alias="businessMeaning", max_length=200)
    notes: str = Field(default="", max_length=500)
    reviewer: str = Field(default="人工审核", max_length=100)


class SemanticStateReplayRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    subject_type: str | None = Field(default=None, alias="subjectType", max_length=100)
    subject_key: str | None = Field(default=None, alias="subjectKey", max_length=200)


class SemanticIdentityRevokeRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    reviewer: str = Field(default="人工审核", max_length=100)
    reason: str = Field(min_length=3, max_length=500)
    idempotency_key: str = Field(alias="idempotencyKey", min_length=3, max_length=200)


class SemanticIdentityReviewRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    decision: Literal["approved", "rejected"]
    reviewer: str = Field(default="人工审核", max_length=100)
    note: str = Field(default="", max_length=500)
    evidence: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str = Field(alias="idempotencyKey", min_length=3, max_length=200)


class CanonicalSparqlRequest(BaseModel):
    query: str = Field(min_length=1, max_length=20000)


class SemanticReleaseApprovalRequest(BaseModel):
    reviewer: str = Field(default="人工审核", min_length=1, max_length=100)
    receipt: str = Field(min_length=3, max_length=300)
    note: str = Field(default="", max_length=500)


# Phase 1 compatibility bridge: route annotations now use the domain-owned
# schema modules. The legacy in-file declarations above remain temporarily
# for import compatibility and are removed after route extraction is complete.
from app.schemas.review import FormalBatchApprovalRequest, ReviewRequest, ReviewResponse
from app.schemas.cleaning import (
    CleaningBatchApprovalRequest,
    CleaningBatchPublishRequest,
    CleaningRuleRegistrationRequest,
    CleaningTaskActionRequest,
    CleaningTaskAdvanceRequest,
    CleaningTaskPreviewRequest,
)
from app.schemas.rule_agent import (
    RuleAgentProposalActionRequest,
    RuleAgentReviewRequest,
    RuleAgentRunRequest,
    SemanticReasoningRequest,
)
from app.schemas.ai_review import (
    AgentCandidateReviewRequest,
    AiBulkDecisionRequest,
    AiClusterDecisionRequest,
    AiDecisionRequest,
    AutoApprovalRequest,
)
from app.schemas.semantic import (
    CanonicalSparqlRequest,
    DefectStatusReviewRequest,
    SemanticActionApprovalRequest,
    SemanticExecutionDispatchRequest,
    SemanticExecutionPreviewRequest,
    SemanticIdentityRevokeRequest,
    SemanticIdentityReviewRequest,
    SemanticStateReplayRequest,
)


AUTO_APPROVAL_POLICY_VERSION = "conservative-equivalence-v1"


def auto_approval_filter(batch_id: str, sample_id: str | None = None) -> tuple[str, list[Any]]:
    where = [
        "c.batch_id = ?",
        "c.review_state = 'pending'",
        "c.validator_status = 'candidate'",
        "c.confidence = 'high'",
        "c.evidence_level = 'strong'",
        "length(trim(c.original_description)) > 0",
        "length(trim(c.candidate_description)) > 0",
        "trim(c.original_description) = trim(c.candidate_description)",
        "c.reason_codes_json NOT LIKE '%CONFLICT%'",
        "c.reason_codes_json NOT LIKE '%BLOCK%'",
    ]
    parameters: list[Any] = [batch_id]
    if sample_id:
        where.append("EXISTS (SELECT 1 FROM review_sample_item si WHERE si.sample_id=? AND si.candidate_id=c.candidate_id)")
        parameters.append(sample_id)
    return " AND ".join(where), parameters


def auto_approval_preview(connection: sqlite3.Connection, scope: Literal["sample", "batch"]) -> dict[str, Any]:
    batch = latest_batch(connection)
    sample_id = None
    selected_count = None
    if scope == "sample":
        sample = ensure_review_sample(connection, batch)
        sample_id = sample["sample_id"]
        selected_count = int(sample["selected_count"])
    where_sql, parameters = auto_approval_filter(batch["batch_id"], sample_id)
    eligible = int(connection.execute(
        f"SELECT count(*) FROM semantic_candidate c WHERE {where_sql}", parameters
    ).fetchone()[0])
    pending_sql = "SELECT count(*) FROM semantic_candidate WHERE batch_id=? AND review_state='pending'"
    pending_parameters: list[Any] = [batch["batch_id"]]
    if sample_id:
        pending_sql = """SELECT count(*) FROM semantic_candidate c
          WHERE c.batch_id=? AND c.review_state='pending'
            AND EXISTS (SELECT 1 FROM review_sample_item si WHERE si.sample_id=? AND si.candidate_id=c.candidate_id)"""
        pending_parameters.append(sample_id)
    pending = int(connection.execute(pending_sql, pending_parameters).fetchone()[0])
    return {
        "scope": scope,
        "batchId": batch["batch_id"],
        "sampleId": sample_id,
        "sampleSelectedCount": selected_count,
        "eligibleCount": eligible,
        "pendingCount": pending,
        "policyVersion": AUTO_APPROVAL_POLICY_VERSION,
        "engine": "deterministic-ai-gate",
        "rules": [
            "仅处理待审核记录",
            "validator_status=candidate、confidence=high、evidence_level=strong",
            "原始描述与候选描述去首尾空格后完全一致",
            "排除 CONFLICT/BLOCK 原因码，源 MaxiEAM 保持只读",
        ],
    }


@domain_get("/api/ai-review/preview")
def ai_review_preview(scope: Literal["sample", "batch"] = "sample") -> dict[str, Any]:
    sqlite = sqlite_connection()
    try:
        return auto_approval_preview(sqlite, scope)
    finally:
        sqlite.close()




@domain_get("/api/ai-review/agent-preview")
def ai_agent_review_preview(
    batch_size: int = Query(default=CANDIDATE_AGENT_DEFAULT_BATCH_SIZE, ge=10, le=CANDIDATE_AGENT_MAX_BATCH_SIZE),
) -> dict[str, Any]:
    """Show the next AI-review workload without calling the model or writing state."""
    sqlite = sqlite_connection()
    try:
        batch = latest_batch(sqlite)
        return candidate_agent_preview(sqlite, batch, batch_size)
    finally:
        sqlite.close()


@domain_post("/api/ai-review/auto-approve")
def ai_auto_approve(request: AutoApprovalRequest) -> dict[str, Any]:
    sqlite = sqlite_connection()
    try:
        existing = sqlite.execute(
            "SELECT * FROM ai_review_run WHERE idempotency_key=?",
            (request.idempotency_key,),
        ).fetchone()
        if existing is not None:
            return {
                "runId": existing["run_id"],
                "batchId": existing["batch_id"],
                "scope": existing["scope"],
                "policyVersion": existing["policy_version"],
                "eligibleCount": int(existing["eligible_count"]),
                "appliedCount": int(existing["applied_count"]),
                "skippedCount": int(existing["skipped_count"]),
                "status": existing["status"],
                "replayed": True,
            }

        batch = latest_batch(sqlite)
        sample_id = None
        if request.scope == "sample":
            sample = ensure_review_sample(sqlite, batch)
            sample_id = sample["sample_id"]
        where_sql, parameters = auto_approval_filter(batch["batch_id"], sample_id)
        eligible_rows = sqlite.execute(
            f"""
            SELECT c.candidate_id, c.candidate_description
            FROM semantic_candidate c
            WHERE {where_sql}
            ORDER BY c.candidate_id
            """,
            parameters,
        ).fetchall()
        now = utc_now()
        run_id = f"ai-review-{uuid.uuid4().hex}"
        applied = 0
        skipped = 0
        sqlite.execute("BEGIN IMMEDIATE")
        for row in eligible_rows:
            current = sqlite.execute(
                "SELECT review_state FROM semantic_candidate WHERE candidate_id=?",
                (row["candidate_id"],),
            ).fetchone()
            if current is None or current["review_state"] != "pending":
                skipped += 1
                continue
            review_id = f"review-{uuid.uuid4().hex}"
            receipt = f"receipt-{uuid.uuid4().hex}"
            sqlite.execute(
                """
                INSERT INTO review_decision
                  (review_id,candidate_id,decision,reviewed_description,reason_code,review_note,reviewer,approval_receipt,reviewed_at)
                VALUES (?,?,?,?,?,?,?,?,?)
                """,
                (
                    review_id,
                    row["candidate_id"],
                    "approved",
                    row["candidate_description"],
                    "AI_AUTO_APPROVE_EQUIVALENT",
                    f"AI 辅助审批；策略 {AUTO_APPROVAL_POLICY_VERSION}；原描述与候选描述一致，未改写源数据。",
                    "ai-gate",
                    receipt,
                    now,
                ),
            )
            sqlite.execute(
                "UPDATE semantic_candidate SET review_state='approved' WHERE candidate_id=?",
                (row["candidate_id"],),
            )
            sqlite.execute(
                """
                INSERT INTO audit_event(entity_type,entity_id,event_type,actor,payload_json,event_at)
                VALUES (?,?,?,?,?,?)
                """,
                (
                    "review",
                    review_id,
                    "ai_auto_approved",
                    "ai-gate",
                    json.dumps({
                        "candidate_id": row["candidate_id"],
                        "policy_version": AUTO_APPROVAL_POLICY_VERSION,
                        "scope": request.scope,
                        "run_id": run_id,
                    }, ensure_ascii=False),
                    now,
                ),
            )
            applied += 1

        sqlite.execute(
            """
            UPDATE batch_run SET
              needs_review_count=(SELECT count(*) FROM semantic_candidate WHERE batch_id=? AND review_state='pending'),
              approved_count=(SELECT count(*) FROM semantic_candidate WHERE batch_id=? AND review_state IN ('approved','modified'))
            WHERE batch_id=?
            """,
            (batch["batch_id"], batch["batch_id"], batch["batch_id"]),
        )
        sqlite.execute(
            """
            UPDATE review_sample
            SET status=CASE WHEN NOT EXISTS (
              SELECT 1 FROM review_sample_item i
              JOIN semantic_candidate c ON c.candidate_id=i.candidate_id
              WHERE i.sample_id=review_sample.sample_id AND c.review_state='pending'
            ) THEN 'completed' ELSE status END
            WHERE sample_id=?
            """,
            (sample_id,) if sample_id else ("",),
        )
        sqlite.execute(
            """
            INSERT INTO ai_review_run
              (run_id,idempotency_key,batch_id,scope,policy_version,eligible_count,applied_count,skipped_count,status,started_at,finished_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            (run_id, request.idempotency_key, batch["batch_id"], request.scope, AUTO_APPROVAL_POLICY_VERSION, len(eligible_rows), applied, skipped, "completed", now, now),
        )
        sqlite.execute(
            """
            INSERT INTO audit_event(entity_type,entity_id,event_type,actor,payload_json,event_at)
            VALUES (?,?,?,?,?,?)
            """,
            (
                "ai_review_run",
                run_id,
                "ai_auto_approval_completed",
                "ai-gate",
                json.dumps({"scope": request.scope, "eligible_count": len(eligible_rows), "applied_count": applied, "skipped_count": skipped, "policy_version": AUTO_APPROVAL_POLICY_VERSION}, ensure_ascii=False),
                now,
            ),
        )
        sqlite.commit()
        return {
            "runId": run_id,
            "batchId": batch["batch_id"],
            "scope": request.scope,
            "policyVersion": AUTO_APPROVAL_POLICY_VERSION,
            "eligibleCount": len(eligible_rows),
            "appliedCount": applied,
            "skippedCount": skipped,
            "status": "completed",
            "replayed": False,
        }
    except HTTPException:
        if sqlite.in_transaction:
            sqlite.rollback()
        raise
    except sqlite3.IntegrityError as exc:
        if sqlite.in_transaction:
            sqlite.rollback()
        raise HTTPException(status_code=409, detail=f"AI 辅助审批写入冲突：{exc}") from exc
    finally:
        sqlite.close()


@domain_post("/api/ai-review/agent-audit")
def agent_audit_pending_candidates(request: AgentCandidateReviewRequest) -> dict[str, Any]:
    """Audit one bounded high-quality slice with the model.

    The agent may only approve retention of the source description. Every
    other answer is isolated as ``deferred`` for later human review; it never
    becomes an automatic rejection or publication.
    """
    sqlite = sqlite_connection()
    try:
        existing = sqlite.execute(
            "SELECT * FROM ai_review_run WHERE idempotency_key=?",
            (request.idempotency_key,),
        ).fetchone()
        if existing is not None:
            event = sqlite.execute(
                "SELECT payload_json FROM audit_event WHERE entity_type='ai_review_run' AND entity_id=? AND event_type='candidate_agent_review_completed' ORDER BY event_id DESC LIMIT 1",
                (existing["run_id"],),
            ).fetchone()
            payload = json.loads(event["payload_json"]) if event else {}
            return {**payload, "runId": existing["run_id"], "replayed": True}

        batch = latest_batch(sqlite)
        where_sql, parameters = candidate_agent_where(
            batch["batch_id"],
            candidate_ids=request.candidate_ids,
        )
        parameters.append(request.batch_size)
        rows = sqlite.execute(
            f"""
            SELECT candidate_id,original_description,candidate_description,semantic_action,
              confidence,evidence_level,validator_status,reason_codes_json,
              site_id,asset_number,location_code,location_description,location_parent,
              classification_description
            FROM (
              SELECT c.candidate_id,c.original_description,c.candidate_description,c.semantic_action,
                c.confidence,c.evidence_level,c.validator_status,c.reason_codes_json,
                d.site_id,d.asset_number,d.location_code,d.location_description,d.location_parent,
                d.classification_description,
                ROW_NUMBER() OVER (PARTITION BY d.site_id ORDER BY d.asset_number,c.candidate_id) AS agent_site_rank
              FROM semantic_candidate c JOIN device_identity d ON d.device_id=c.device_id
              WHERE {where_sql}
            ) eligible
            ORDER BY agent_site_rank,site_id,asset_number,candidate_id
            LIMIT ?
            """,
            parameters,
        ).fetchall()
        raw_items = invoke_pending_candidate_review(rows) if rows else []
        decisions = canonicalize_pending_candidate_reviews(raw_items, rows)
        decision_by_id = {item["candidateId"]: item for item in decisions}
        run_id = f"ai-agent-review-{uuid.uuid4().hex}"
        now = utc_now()
        approved_count = 0
        needs_review_count = 0
        rejected_count = 0
        isolated_count = 0
        skipped_count = 0
        sqlite.execute("BEGIN IMMEDIATE")
        for row in rows:
            item = decision_by_id[str(row["candidate_id"])]
            if item["decision"] == "approved":
                existing_review = sqlite.execute(
                    "SELECT review_id FROM review_decision WHERE candidate_id=?",
                    (row["candidate_id"],),
                ).fetchone()
                if existing_review is not None:
                    skipped_count += 1
                    continue
                review_id = f"review-{uuid.uuid4().hex}"
                receipt = f"receipt-{uuid.uuid4().hex}"
                note = json.dumps(
                    {
                        "agentDecision": item["agentDecision"],
                        "confidence": item["confidence"],
                        "risk": item["risk"],
                        "reason": item["reason"],
                        "localGate": item["localGate"],
                        "policyVersion": CANDIDATE_AGENT_REVIEW_VERSION,
                    },
                    ensure_ascii=False,
                )
                sqlite.execute(
                    "INSERT INTO review_decision(review_id,candidate_id,decision,reviewed_description,reason_code,review_note,reviewer,approval_receipt,reviewed_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (review_id, row["candidate_id"], "approved", row["original_description"], "AI_AGENT_KEEP_ORIGINAL", note, "semantic-review-agent", receipt, now),
                )
                sqlite.execute(
                    "UPDATE semantic_candidate SET review_state='approved' WHERE candidate_id=?",
                    (row["candidate_id"],),
                )
                sqlite.execute(
                    "INSERT INTO audit_event(entity_type,entity_id,event_type,actor,payload_json,event_at) VALUES (?,?,?,?,?,?)",
                    ("review", review_id, "candidate_agent_review_approved", "semantic-review-agent", json.dumps({"candidateId": row["candidate_id"], **item, "sourceWrite": False, "formalPublication": False}, ensure_ascii=False), now),
                )
                approved_count += 1
            else:
                if item["agentDecision"] == "reject":
                    rejected_count += 1
                needs_review_count += 1
                isolated_count += 1
                # Do not create a review_decision here.  The deferred state is
                # an AI isolation marker, so a human can still submit the
                # first real review decision later without a UNIQUE conflict.
                sqlite.execute(
                    "UPDATE semantic_candidate SET review_state='deferred' WHERE candidate_id=? AND review_state='pending'",
                    (row["candidate_id"],),
                )
                sqlite.execute(
                    "INSERT INTO audit_event(entity_type,entity_id,event_type,actor,payload_json,event_at) VALUES (?,?,?,?,?,?)",
                    ("candidate", row["candidate_id"], "candidate_agent_review_isolated", "semantic-review-agent", json.dumps({"candidateId": row["candidate_id"], **item, "isolation": "deferred", "sourceWrite": False, "formalPublication": False}, ensure_ascii=False), now),
                )
        sqlite.execute(
            "UPDATE batch_run SET needs_review_count=(SELECT count(*) FROM semantic_candidate WHERE batch_id=? AND review_state='pending'), approved_count=(SELECT count(*) FROM semantic_candidate WHERE batch_id=? AND review_state IN ('approved','modified')) WHERE batch_id=?",
            (batch["batch_id"], batch["batch_id"], batch["batch_id"]),
        )
        result = {
            "runId": run_id,
            "batchId": batch["batch_id"],
            "policyVersion": CANDIDATE_AGENT_REVIEW_VERSION,
            "candidateCount": len(rows),
            "approvedCount": approved_count,
            "needsReviewCount": needs_review_count,
            "rejectedCount": rejected_count,
            "isolatedCount": isolated_count,
            "skippedCount": skipped_count,
            "status": "completed",
            "decisions": decisions,
            "sourceWrite": False,
            "formalPublication": False,
        }
        sqlite.execute(
            "INSERT INTO ai_review_run(run_id,idempotency_key,batch_id,scope,policy_version,eligible_count,applied_count,skipped_count,status,started_at,finished_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, request.idempotency_key, batch["batch_id"], "batch", CANDIDATE_AGENT_REVIEW_VERSION, len(rows), approved_count, skipped_count + isolated_count, "completed", now, now),
        )
        sqlite.execute(
            "INSERT INTO audit_event(entity_type,entity_id,event_type,actor,payload_json,event_at) VALUES (?,?,?,?,?,?)",
            ("ai_review_run", run_id, "candidate_agent_review_completed", "semantic-review-agent", json.dumps({**result, "note": request.note}, ensure_ascii=False), now),
        )
        sqlite.commit()
        refreshed_preview = candidate_agent_preview(sqlite, batch, request.batch_size)
        result.update({
            "remainingEligibleCount": refreshed_preview["eligibleCount"],
            "remainingPendingCount": refreshed_preview["pendingCount"],
            "isolatedTotalCount": refreshed_preview["isolatedCount"],
        })
        return result
    except HTTPException:
        if sqlite.in_transaction:
            sqlite.rollback()
        raise
    except sqlite3.IntegrityError as exc:
        if sqlite.in_transaction:
            sqlite.rollback()
        raise HTTPException(status_code=409, detail=f"candidate agent review write conflict: {exc}") from exc
    finally:
        sqlite.close()


def health() -> dict[str, Any]:
    sqlite_ok = SQLITE_DB.exists()
    duckdb_ok = DUCKDB_DB.exists()
    canonical_ok = CANONICAL_SEMANTICS_DB.exists()
    active_release = load_active_release()
    return {
        "status": "ok" if sqlite_ok and duckdb_ok and canonical_ok else "degraded",
        "sqlite": sqlite_ok,
        "duckdb": duckdb_ok,
        "canonicalRdf": canonical_ok,
        "activeRelease": active_release,
        "metricSeries": len(runtime_metrics.snapshot()["counters"]) + len(runtime_metrics.snapshot()["observations"]),
        "sourceWrite": False,
        "formalPublication": False,
    }


@domain_get("/api/unified-devices/summary")
def unified_device_summary() -> dict[str, Any]:
    identity = identity_result_connection()
    overlay = unified_semantics_connection()
    try:
        run = overlay.execute(
            "SELECT * FROM semantic_layer_run ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        if run is None:
            raise HTTPException(status_code=503, detail="统一设备语义覆盖层没有运行批次")
        map_counts = {
            row["status"]: int(row["count"])
            for row in identity.execute(
                "SELECT status,count(*) AS count FROM device_identity_map GROUP BY status"
            ).fetchall()
        }
        relation_counts = {
            row["status"]: int(row["count"])
            for row in overlay.execute(
                "SELECT status,count(*) AS count FROM unified_device_relation GROUP BY status"
            ).fetchall()
        }
        link_counts: dict[str, dict[str, int]] = {}
        for row in overlay.execute(
            "SELECT business_type,status,count(*) AS count FROM business_record_link GROUP BY business_type,status"
        ).fetchall():
            link_counts.setdefault(row["business_type"], {})[row["status"]] = int(row["count"])
        event_counts: dict[str, dict[str, int]] = {}
        for row in identity.execute(
            "SELECT event_type,link_status,count(*) AS count FROM device_event GROUP BY event_type,link_status"
        ).fetchall():
            event_counts.setdefault(row["event_type"], {})[row["link_status"]] = int(row["count"])
        source_systems = [dict(row) for row in overlay.execute(
            "SELECT system_key,display_name,connection_kind,snapshot_id,read_only,status FROM source_system ORDER BY system_key"
        ).fetchall()]
        return {
            "runId": run["run_id"],
            "sourceSnapshotId": run["source_snapshot_id"],
            "identityDatabase": run["identity_db_path"],
            "unifiedDeviceCount": int(run["unified_device_count"]),
            "identityMapCount": int(run["identity_map_count"]),
            "identityMapByStatus": map_counts,
            "relationCount": int(run["relation_count"]),
            "relationByStatus": relation_counts,
            "businessLinkCount": int(run["business_link_count"]),
            "acceptedBusinessLinkCount": int(run["accepted_business_link_count"]),
            "businessLinksByType": link_counts,
            "businessEventCount": int(identity.execute("SELECT count(*) FROM device_event").fetchone()[0]),
            "businessEventsByTypeStatus": event_counts,
            "crossSystemCandidateCount": int(identity.execute("SELECT count(*) FROM cross_system_match_candidate").fetchone()[0]),
            "sourceSystems": source_systems,
            "sourceWrite": False,
            "formalPublication": False,
            "policy": [
                "源系统和源表只读",
                "统一设备 ID 独立于 HD/XNY 的源编码",
                "跨系统映射不自动合并，必须保留证据和审核状态",
                "巡检、缺陷、工单仅通过本地业务记录挂接表关联",
            ],
        }
    finally:
        overlay.close()
        identity.close()


def unified_device_list_row(
    row: sqlite3.Row,
    mapping_count: int,
    link_count: int,
    relation_count: int,
    accepted_link_count: int = 0,
    review_link_count: int = 0,
) -> dict[str, Any]:
    return {
        "unifiedDeviceId": row["unified_device_id"],
        "sourceSchema": row["master_source_schema"],
        "siteId": row["site_id"],
        "assetNumber": row["asset_number"],
        "sourceAssetId": row["source_asset_id"] or "",
        "canonicalName": row["canonical_name"] or "",
        "locationCode": row["location_code"] or "",
        "parentAssetNumber": row["parent_asset_number"] or "",
        "organization": row["org_id"] or "",
        "classificationId": row["classification_id"] or "",
        "status": row["status"] or "",
        "seedStatus": row["seed_status"],
        "mappingCount": mapping_count,
        "businessLinkCount": link_count,
        "acceptedBusinessLinkCount": accepted_link_count,
        "businessReviewCount": review_link_count,
        "relationCount": relation_count,
        "mappingStatus": "已挂接业务记录" if accepted_link_count else ("业务记录待确认" if review_link_count else ("已有身份映射" if mapping_count else "仅设备主数据")),
        "sourceSnapshotId": row["source_snapshot_id"],
    }


def unified_location_row(row: sqlite3.Row, device_count: int, business_record_count: int) -> dict[str, Any]:
    return {
        "locationRecordId": row["location_record_id"],
        "sourceSchema": row["source_schema"],
        "siteId": row["site_id"],
        "locationCode": row["location_code"],
        "sourceLocationId": row["source_location_id"] or "",
        "description": row["description"] or "",
        "parentLocation": row["parent_location"] or "",
        "status": row["status"] or "",
        "classificationId": row["classstructure_id"] or "",
        "deviceCount": device_count,
        "businessRecordCount": business_record_count,
        "sourceSnapshotId": row["source_snapshot_id"],
    }


@domain_get("/api/unified-devices")
def unified_devices(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    search: str | None = None,
    source_schema: Literal["all", "HD_SAAS", "XNY_SAAS"] = "all",
    site_id: str | None = None,
) -> dict[str, Any]:
    identity = identity_result_connection()
    overlay = unified_semantics_connection()
    try:
        where: list[str] = []
        parameters: list[Any] = []
        if source_schema != "all":
            where.append("master_source_schema=?")
            parameters.append(source_schema)
        if site_id:
            where.append("site_id=?")
            parameters.append(site_id)
        if search and search.strip():
            value = f"%{search.strip()}%"
            where.append("(asset_number LIKE ? OR canonical_name LIKE ? OR location_code LIKE ? OR source_asset_id LIKE ?)")
            parameters.extend([value] * 4)
        where_sql = f"WHERE {' AND '.join(where)}" if where else ""
        total = int(identity.execute(f"SELECT count(*) FROM unified_device {where_sql}", parameters).fetchone()[0])
        rows = identity.execute(
            f"""
            SELECT unified_device_id,master_source_schema,site_id,asset_number,source_asset_id,
              canonical_name,location_code,parent_asset_number,org_id,classification_id,status,
              seed_status,source_snapshot_id
            FROM unified_device
            {where_sql}
            ORDER BY master_source_schema,site_id,asset_number,unified_device_id
            LIMIT ? OFFSET ?
            """,
            parameters + [page_size, (page - 1) * page_size],
        ).fetchall()
        if not rows:
            return {"rows": [], "total": total, "page": page, "pageSize": page_size}
        ids = [row["unified_device_id"] for row in rows]
        marks = ",".join("?" for _ in ids)
        map_counts = {
            row["unified_device_id"]: int(row["count"])
            for row in identity.execute(
                f"SELECT unified_device_id,count(*) AS count FROM device_identity_map WHERE unified_device_id IN ({marks}) GROUP BY unified_device_id",
                ids,
            ).fetchall()
        }
        link_counts = {
            row["unified_device_id"]: int(row["count"])
            for row in overlay.execute(
                f"SELECT unified_device_id,count(*) AS count FROM business_record_link WHERE unified_device_id IN ({marks}) GROUP BY unified_device_id",
                ids,
            ).fetchall()
        }
        accepted_link_counts = {
            row["unified_device_id"]: int(row["count"])
            for row in overlay.execute(
                f"SELECT unified_device_id,count(*) AS count FROM business_record_link WHERE status='accepted' AND unified_device_id IN ({marks}) GROUP BY unified_device_id",
                ids,
            ).fetchall()
        }
        review_link_counts = {
            row["unified_device_id"]: int(row["count"])
            for row in overlay.execute(
                f"SELECT unified_device_id,count(*) AS count FROM business_record_link WHERE status IN ('needs_review','blocked') AND unified_device_id IN ({marks}) GROUP BY unified_device_id",
                ids,
            ).fetchall()
        }
        relation_counts = {
            row["unified_device_id"]: int(row["count"])
            for row in overlay.execute(
                f"SELECT subject_unified_device_id AS unified_device_id,count(*) AS count FROM unified_device_relation WHERE subject_unified_device_id IN ({marks}) GROUP BY subject_unified_device_id",
                ids,
            ).fetchall()
        }
        return {
            "rows": [unified_device_list_row(row, map_counts.get(row["unified_device_id"], 0), link_counts.get(row["unified_device_id"], 0), relation_counts.get(row["unified_device_id"], 0), accepted_link_counts.get(row["unified_device_id"], 0), review_link_counts.get(row["unified_device_id"], 0)) for row in rows],
            "total": total,
            "page": page,
            "pageSize": page_size,
        }
    finally:
        overlay.close()
        identity.close()


@domain_get("/api/unified-devices/{unified_device_id}")
def unified_device_detail(unified_device_id: str) -> dict[str, Any]:
    identity = identity_result_connection()
    overlay = unified_semantics_connection()
    try:
        row = identity.execute(
            "SELECT * FROM unified_device WHERE unified_device_id=?",
            (unified_device_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="统一设备对象不存在")
        mappings = [dict(item) for item in identity.execute(
            """
            SELECT source_schema,source_table_group,source_table,source_row_id,source_key_type,
              source_key,site_id,raw_description,location_code,match_method,match_confidence,
              status,evidence_json,source_snapshot_id
            FROM device_identity_map WHERE unified_device_id=? ORDER BY source_schema,source_table_group,source_table,source_row_id
            """,
            (unified_device_id,),
        ).fetchall()]
        links = [dict(item) for item in overlay.execute(
            """
            SELECT link_id,source_schema,source_table_group,source_table,source_row_id,business_type,
              source_key_type,source_key,status,confidence,evidence_json,source_snapshot_id
            FROM business_record_link WHERE unified_device_id=? ORDER BY business_type,source_schema,source_table,source_row_id
            """,
            (unified_device_id,),
        ).fetchall()]
        relations = [dict(item) for item in overlay.execute(
            """
            SELECT relation_id,subject_unified_device_id,predicate,object_unified_device_id,
              source_schema,source_table,source_row_id,confidence,status,evidence_json,source_snapshot_id
            FROM unified_device_relation
            WHERE subject_unified_device_id=? OR object_unified_device_id=?
            ORDER BY predicate,relation_id
            """,
            (unified_device_id, unified_device_id),
        ).fetchall()]
        object_ids = sorted({item["object_unified_device_id"] for item in relations if item["object_unified_device_id"]})
        related: list[dict[str, Any]] = []
        if object_ids:
            marks = ",".join("?" for _ in object_ids)
            related = [dict(item) for item in identity.execute(
                f"SELECT unified_device_id,master_source_schema,site_id,asset_number,canonical_name FROM unified_device WHERE unified_device_id IN ({marks})",
                object_ids,
            ).fetchall()]
        location: dict[str, Any] | None = None
        location_hierarchy: list[dict[str, Any]] = []
        location_code = (row["location_code"] or "").strip()
        if location_code:
            location_row = identity.execute(
                """
                SELECT * FROM function_location
                WHERE source_schema=? AND site_id=? AND location_code=?
                ORDER BY changed_at DESC, location_record_id
                LIMIT 1
                """,
                (row["master_source_schema"], row["site_id"], location_code),
            ).fetchone()
            if location_row:
                location = {
                    "locationRecordId": location_row["location_record_id"],
                    "sourceSchema": location_row["source_schema"],
                    "siteId": location_row["site_id"],
                    "locationCode": location_row["location_code"],
                    "sourceLocationId": location_row["source_location_id"] or "",
                    "description": location_row["description"] or "",
                    "parentLocation": location_row["parent_location"] or "",
                    "status": location_row["status"] or "",
                    "classificationId": location_row["classstructure_id"] or "",
                    "sourceSnapshotId": location_row["source_snapshot_id"],
                }
            location_hierarchy = [dict(item) for item in identity.execute(
                """
                SELECT hierarchy_record_id,source_schema,site_id,location_code,parent_location,
                  source_hierarchy_id,org_id,source_snapshot_id
                FROM location_hierarchy
                WHERE source_schema=? AND site_id=? AND location_code=?
                ORDER BY hierarchy_record_id
                """,
                (row["master_source_schema"], row["site_id"], location_code),
            ).fetchall()]
        business_record_evidence = [dict(item) for item in identity.execute(
            """
            SELECT event_record_id,unified_device_id,source_schema,event_type,source_table,source_row_id,
              site_id,location_code,event_time,status,description,source_snapshot_id,link_status,evidence_json
            FROM device_event
            WHERE unified_device_id=?
            ORDER BY event_type,event_record_id
            """,
            (unified_device_id,),
        ).fetchall()]
        return {
            "device": unified_device_list_row(
                row,
                len(mappings),
                len(links),
                len(relations),
                sum(1 for item in links if item["status"] == "accepted"),
                sum(1 for item in links if item["status"] in {"needs_review", "blocked"}),
            ),
            "sourceIdentity": {
                "sourceIdentityKey": row["source_identity_key"],
                "sourceAssetId": row["source_asset_id"] or "",
                "masterSourceSchema": row["master_source_schema"],
            },
            "mappings": mappings,
            "businessLinks": links,
            "relations": relations,
            "relatedDevices": related,
            "location": location,
            "locationHierarchy": location_hierarchy,
            "businessRecordEvidence": business_record_evidence,
            "sourceWrite": False,
            "formalPublication": False,
        }
    finally:
        overlay.close()
        identity.close()


@domain_get("/api/unified-locations/summary")
def unified_location_summary() -> dict[str, Any]:
    identity = identity_result_connection()
    try:
        source_counts = [
            {"sourceSchema": row["source_schema"], "count": int(row["count"])}
            for row in identity.execute(
                "SELECT source_schema,count(*) AS count FROM function_location GROUP BY source_schema ORDER BY source_schema"
            ).fetchall()
        ]
        hierarchy_counts = [
            {"sourceSchema": row["source_schema"], "count": int(row["count"])}
            for row in identity.execute(
                "SELECT source_schema,count(*) AS count FROM location_hierarchy GROUP BY source_schema ORDER BY source_schema"
            ).fetchall()
        ]
        return {
            "locationCount": int(identity.execute("SELECT count(*) FROM function_location").fetchone()[0]),
            "hierarchyCount": int(identity.execute("SELECT count(*) FROM location_hierarchy").fetchone()[0]),
            "sourceCounts": source_counts,
            "hierarchyBySource": hierarchy_counts,
            "sourceWrite": False,
            "formalPublication": False,
            "policy": [
                "位置对象来自源快照的 function_location 和 location_hierarchy",
                "设备只通过 source_schema + SITEID + LOCATION 关联位置",
                "位置层级缺失时保留空层级，不自动推断父级",
            ],
        }
    finally:
        identity.close()


@domain_get("/api/unified-locations")
def unified_locations(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    search: str | None = None,
    source_schema: Literal["all", "HD_SAAS", "XNY_SAAS"] = "all",
    site_id: str | None = None,
) -> dict[str, Any]:
    identity = identity_result_connection()
    try:
        where: list[str] = []
        parameters: list[Any] = []
        if source_schema != "all":
            where.append("source_schema=?")
            parameters.append(source_schema)
        if site_id and site_id.strip():
            where.append("site_id=?")
            parameters.append(site_id.strip())
        if search and search.strip():
            value = f"%{search.strip()}%"
            where.append("(location_code LIKE ? OR description LIKE ? OR parent_location LIKE ? OR source_location_id LIKE ?)")
            parameters.extend([value] * 4)
        where_sql = f"WHERE {' AND '.join(where)}" if where else ""
        total = int(identity.execute(f"SELECT count(*) FROM function_location {where_sql}", parameters).fetchone()[0])
        rows = identity.execute(
            f"""
            SELECT location_record_id,source_schema,site_id,location_code,source_location_id,
              description,parent_location,status,classstructure_id,source_snapshot_id
            FROM function_location
            {where_sql}
            ORDER BY source_schema,site_id,location_code,location_record_id
            LIMIT ? OFFSET ?
            """,
            parameters + [page_size, (page - 1) * page_size],
        ).fetchall()
        if not rows:
            return {"rows": [], "total": total, "page": page, "pageSize": page_size}
        keys = [(row["source_schema"], row["site_id"], row["location_code"]) for row in rows]
        device_counts: dict[tuple[str, str, str], int] = {}
        business_counts: dict[tuple[str, str, str], int] = {}
        for source, site, code in keys:
            key = (source, site, code)
            device_counts[key] = int(identity.execute(
                "SELECT count(*) FROM unified_device WHERE master_source_schema=? AND site_id=? AND location_code=?",
                key,
            ).fetchone()[0])
            business_counts[key] = int(identity.execute(
                "SELECT count(*) FROM device_event WHERE source_schema=? AND site_id=? AND location_code=?",
                key,
            ).fetchone()[0])
        return {
            "rows": [unified_location_row(row, device_counts.get((row["source_schema"], row["site_id"], row["location_code"]), 0), business_counts.get((row["source_schema"], row["site_id"], row["location_code"]), 0)) for row in rows],
            "total": total,
            "page": page,
            "pageSize": page_size,
            "sourceWrite": False,
            "formalPublication": False,
        }
    finally:
        identity.close()


@domain_get("/api/unified-locations/{location_record_id}")
def unified_location_detail(location_record_id: str) -> dict[str, Any]:
    identity = identity_result_connection()
    try:
        location = identity.execute(
            "SELECT * FROM function_location WHERE location_record_id=?",
            (location_record_id,),
        ).fetchone()
        if location is None:
            raise HTTPException(status_code=404, detail="统一位置对象不存在")
        hierarchy = [dict(row) for row in identity.execute(
            """
            SELECT hierarchy_record_id,source_schema,site_id,location_code,parent_location,
              source_hierarchy_id,org_id,source_snapshot_id
            FROM location_hierarchy
            WHERE source_schema=? AND site_id=? AND location_code=?
            ORDER BY hierarchy_record_id
            """,
            (location["source_schema"], location["site_id"], location["location_code"]),
        ).fetchall()]
        devices = [dict(row) for row in identity.execute(
            """
            SELECT unified_device_id,master_source_schema,site_id,asset_number,canonical_name,
              location_code,parent_asset_number,status
            FROM unified_device
            WHERE master_source_schema=? AND site_id=? AND location_code=?
            ORDER BY asset_number,unified_device_id
            LIMIT 200
            """,
            (location["source_schema"], location["site_id"], location["location_code"]),
        ).fetchall()]
        business_records = [dict(row) for row in identity.execute(
            """
            SELECT event_record_id,unified_device_id,event_type,source_table,source_row_id,
              site_id,location_code,event_time,status,description,link_status,evidence_json
            FROM device_event
            WHERE source_schema=? AND site_id=? AND location_code=?
            ORDER BY event_type,event_record_id
            LIMIT 200
            """,
            (location["source_schema"], location["site_id"], location["location_code"]),
        ).fetchall()]
        key = (location["source_schema"], location["site_id"], location["location_code"])
        return {
            "location": unified_location_row(location, len(devices), len(business_records)),
            "hierarchy": hierarchy,
            "devices": devices,
            "businessRecords": business_records,
            "sourceWrite": False,
            "formalPublication": False,
        }
    finally:
        identity.close()


def knowledge_asset_row(row: sqlite3.Row, source_count: int, issue_count: int) -> dict[str, Any]:
    return {
        "assetId": row["asset_id"],
        "assetKey": row["asset_key"],
        "assetType": row["asset_type"],
        "title": row["title"],
        "canonicalDefinition": row["canonical_definition"],
        "currentVersion": row["current_version"],
        "status": row["status"],
        "sourceScope": row["source_scope"],
        "sourceCount": source_count,
        "issueCount": issue_count,
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }


@domain_get("/api/knowledge-assets/summary")
def knowledge_asset_summary() -> dict[str, Any]:
    connection = unified_semantics_connection()
    try:
        run = connection.execute(
            "SELECT * FROM knowledge_layer_run ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        if run is None:
            raise HTTPException(status_code=503, detail="知识资产关系层没有构建批次")
        by_type = [dict(row) for row in connection.execute(
            "SELECT asset_type AS value,count(*) AS count FROM knowledge_asset GROUP BY asset_type ORDER BY asset_type"
        ).fetchall()]
        by_status = [dict(row) for row in connection.execute(
            "SELECT status AS value,count(*) AS count FROM knowledge_asset GROUP BY status ORDER BY status"
        ).fetchall()]
        issues = [dict(row) for row in connection.execute(
            "SELECT issue_type AS value,count(*) AS count FROM knowledge_asset_issue WHERE status='open' GROUP BY issue_type ORDER BY issue_type"
        ).fetchall()]
        machine_run = connection.execute(
            "SELECT * FROM machine_semantics_run ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        machine_constraints = [dict(row) for row in connection.execute(
            "SELECT on_fail AS value,count(*) AS count FROM machine_constraint_spec GROUP BY on_fail ORDER BY on_fail"
        ).fetchall()]
        object_types = [dict(row) for row in connection.execute(
            "SELECT object_type,display_name,description,parent_object_type,version,status FROM business_object_type ORDER BY object_type"
        ).fetchall()]
        relation_statuses = [dict(row) for row in connection.execute(
            "SELECT status AS value,count(*) AS count FROM business_object_relation GROUP BY status ORDER BY status"
        ).fetchall()]
        return {
            "runId": run["run_id"],
            "assetCount": int(run["asset_count"]),
            "versionCount": int(run["version_count"]),
            "sourceCount": int(run["source_count"]),
            "bindingCount": int(run["binding_count"]),
            "issueCount": int(run["issue_count"]),
            "assetTypes": by_type,
            "statuses": by_status,
            "openIssues": issues,
            "machineContract": dict(machine_run) if machine_run else None,
            "machineConstraintGates": machine_constraints,
            "businessObjectTypes": object_types,
            "businessObjectRelationCount": int(connection.execute("SELECT count(*) FROM business_object_relation").fetchone()[0]),
            "businessObjectRelationStatuses": relation_statuses,
            "sourceWrite": False,
            "formalPublication": False,
            "policy": [
                "知识资产有稳定 asset_key，不以文档文件名作为身份",
                "不同来源保留为 source 记录，差异和冲突不静默覆盖",
                "版本、回放、审批和启用状态分开记录",
                "当前层只写本地关系库，不修改源表或正式结果层",
            ],
        }
    finally:
        connection.close()


@domain_get("/api/knowledge-assets")
def knowledge_assets(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    search: str | None = None,
    asset_type: Literal["all", "rule", "terminology", "evaluation_case", "definition"] = "all",
    status: str | None = None,
) -> dict[str, Any]:
    connection = unified_semantics_connection()
    try:
        where: list[str] = []
        parameters: list[Any] = []
        if asset_type != "all":
            where.append("asset_type=?")
            parameters.append(asset_type)
        if status and status.strip():
            where.append("status=?")
            parameters.append(status.strip())
        if search and search.strip():
            value = f"%{search.strip()}%"
            where.append("(asset_key LIKE ? OR title LIKE ? OR canonical_definition LIKE ? OR current_version LIKE ?)")
            parameters.extend([value] * 4)
        where_sql = f"WHERE {' AND '.join(where)}" if where else ""
        total = int(connection.execute(f"SELECT count(*) FROM knowledge_asset {where_sql}", parameters).fetchone()[0])
        rows = connection.execute(
            f"""
            SELECT * FROM knowledge_asset
            {where_sql}
            ORDER BY updated_at DESC,asset_key
            LIMIT ? OFFSET ?
            """,
            parameters + [page_size, (page - 1) * page_size],
        ).fetchall()
        if not rows:
            return {"items": [], "total": total, "page": page, "pageSize": page_size, "sourceWrite": False, "formalPublication": False}
        ids = [row["asset_id"] for row in rows]
        marks = ",".join("?" for _ in ids)
        source_counts = {row["asset_id"]: int(row["count"]) for row in connection.execute(
            f"SELECT asset_id,count(*) AS count FROM knowledge_asset_source WHERE asset_id IN ({marks}) GROUP BY asset_id", ids
        ).fetchall()}
        issue_counts = {row["asset_id"]: int(row["count"]) for row in connection.execute(
            f"SELECT asset_id,count(*) AS count FROM knowledge_asset_issue WHERE status='open' AND asset_id IN ({marks}) GROUP BY asset_id", ids
        ).fetchall()}
        return {
            "items": [knowledge_asset_row(row, source_counts.get(row["asset_id"], 0), issue_counts.get(row["asset_id"], 0)) for row in rows],
            "total": total,
            "page": page,
            "pageSize": page_size,
            "sourceWrite": False,
            "formalPublication": False,
        }
    finally:
        connection.close()


@domain_get("/api/knowledge-assets/{asset_id}")
def knowledge_asset_detail(asset_id: str) -> dict[str, Any]:
    connection = unified_semantics_connection()
    try:
        asset = connection.execute("SELECT * FROM knowledge_asset WHERE asset_id=?", (asset_id,)).fetchone()
        if asset is None:
            raise HTTPException(status_code=404, detail="知识资产不存在")
        versions = [dict(row) for row in connection.execute(
            "SELECT * FROM knowledge_asset_version WHERE asset_id=? ORDER BY created_at DESC,version DESC",
            (asset_id,),
        ).fetchall()]
        parts = [dict(row) for row in connection.execute(
            """
            SELECT p.* FROM knowledge_asset_part p
            JOIN knowledge_asset_version v ON v.asset_version_id=p.asset_version_id
            WHERE v.asset_id=? ORDER BY v.created_at DESC,p.part_type,p.ordinal
            """,
            (asset_id,),
        ).fetchall()]
        sources = [dict(row) for row in connection.execute(
            "SELECT * FROM knowledge_asset_source WHERE asset_id=? ORDER BY created_at,source_kind,source_record_id",
            (asset_id,),
        ).fetchall()]
        bindings = [dict(row) for row in connection.execute(
            "SELECT * FROM knowledge_asset_binding WHERE asset_id=? ORDER BY object_type,object_key",
            (asset_id,),
        ).fetchall()]
        issues = [dict(row) for row in connection.execute(
            "SELECT * FROM knowledge_asset_issue WHERE asset_id=? ORDER BY status,created_at",
            (asset_id,),
        ).fetchall()]
        contract = connection.execute(
            "SELECT * FROM machine_semantic_contract WHERE asset_version_id IN (SELECT asset_version_id FROM knowledge_asset_version WHERE asset_id=?) ORDER BY created_at DESC LIMIT 1",
            (asset_id,),
        ).fetchone()
        decisions = [dict(row) for row in connection.execute(
            "SELECT * FROM machine_decision_spec WHERE asset_version_id IN (SELECT asset_version_id FROM knowledge_asset_version WHERE asset_id=?) ORDER BY created_at DESC",
            (asset_id,),
        ).fetchall()]
        calculations = [dict(row) for row in connection.execute(
            "SELECT * FROM machine_calculation_spec WHERE asset_version_id IN (SELECT asset_version_id FROM knowledge_asset_version WHERE asset_id=?) ORDER BY created_at DESC",
            (asset_id,),
        ).fetchall()]
        constraints = [dict(row) for row in connection.execute(
            "SELECT * FROM machine_constraint_spec WHERE asset_version_id IN (SELECT asset_version_id FROM knowledge_asset_version WHERE asset_id=?) ORDER BY created_at DESC,constraint_key",
            (asset_id,),
        ).fetchall()]
        actions = [dict(row) for row in connection.execute(
            "SELECT * FROM machine_action_spec WHERE asset_version_id IN (SELECT asset_version_id FROM knowledge_asset_version WHERE asset_id=?) ORDER BY created_at DESC",
            (asset_id,),
        ).fetchall()]
        return {
            "asset": knowledge_asset_row(asset, len(sources), sum(1 for item in issues if item["status"] == "open")),
            "versions": versions,
            "parts": parts,
            "sources": sources,
            "bindings": bindings,
            "issues": issues,
            "machineContract": dict(contract) if contract else None,
            "machineDecisions": decisions,
            "machineCalculations": calculations,
            "machineConstraints": constraints,
            "machineActions": actions,
            "sourceWrite": False,
            "formalPublication": False,
        }
    finally:
        connection.close()


@domain_get("/api/semantic-facts/summary")
def semantic_facts_summary() -> dict[str, Any]:
    """Return the explainable-fact pipeline state from the local read-only layer."""
    connection = unified_semantics_connection()
    try:
        latest_run = connection.execute(
            "SELECT * FROM semantic_fact_layer_run ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        if latest_run is None:
            raise HTTPException(status_code=503, detail="事实语义层没有构建批次")
        fact_statuses = [dict(row) for row in connection.execute(
            "SELECT status AS value,count(*) AS count FROM semantic_fact GROUP BY status ORDER BY status"
        ).fetchall()]
        fact_types = [dict(row) for row in connection.execute(
            "SELECT fact_type AS value,count(*) AS count FROM semantic_fact GROUP BY fact_type ORDER BY fact_type"
        ).fetchall()]
        decision_statuses = [dict(row) for row in connection.execute(
            "SELECT status AS value,count(*) AS count FROM semantic_rule_decision GROUP BY status ORDER BY status"
        ).fetchall()]
        action_statuses = [dict(row) for row in connection.execute(
            "SELECT status AS value,count(*) AS count FROM semantic_escalation_action GROUP BY status ORDER BY status"
        ).fetchall()]
        logic_rule_count = int(connection.execute(
            "SELECT count(*) FROM knowledge_asset WHERE asset_type='rule'"
        ).fetchone()[0])
        logic_ready_count = int(connection.execute(
            """
            SELECT count(*) FROM machine_semantic_contract c
            JOIN knowledge_asset_version v ON v.asset_version_id=c.asset_version_id
            JOIN knowledge_asset a ON a.asset_id=v.asset_id
            WHERE a.asset_type='rule' AND c.status='ready'
            """
        ).fetchone()[0])
        logic_action_spec_count = int(connection.execute(
            """
            SELECT count(*) FROM machine_action_spec s
            JOIN knowledge_asset_version v ON v.asset_version_id=s.asset_version_id
            JOIN knowledge_asset a ON a.asset_id=v.asset_id
            WHERE a.asset_type='rule'
            """
        ).fetchone()[0])
        deterministic_rule_count = int(connection.execute(
            "SELECT count(*) FROM semantic_logic_rule WHERE status='enabled'"
        ).fetchone()[0])
        reasoning_run = connection.execute(
            "SELECT * FROM semantic_reasoning_run ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        return {
            "latestRun": dict(latest_run),
            "factCount": int(connection.execute("SELECT count(*) FROM semantic_fact").fetchone()[0]),
            "observedFactCount": int(connection.execute("SELECT count(*) FROM semantic_fact WHERE status='observed'").fetchone()[0]),
            "derivedFactCount": int(connection.execute("SELECT count(*) FROM semantic_fact WHERE status IN ('derived','accepted')").fetchone()[0]),
            "derivationCount": int(connection.execute("SELECT count(*) FROM semantic_fact_derivation").fetchone()[0]),
            "decisionCount": int(connection.execute("SELECT count(*) FROM semantic_rule_decision").fetchone()[0]),
            "actionCount": int(connection.execute("SELECT count(*) FROM semantic_escalation_action").fetchone()[0]),
            "logicRuleCount": logic_rule_count,
            "logicReadyCount": logic_ready_count,
            "logicActionSpecCount": logic_action_spec_count,
            "deterministicRuleCount": deterministic_rule_count,
            "latestReasoningRun": dict(reasoning_run) if reasoning_run else None,
            "factStatuses": fact_statuses,
            "factTypes": fact_types,
            "decisionStatuses": decision_statuses,
            "actionStatuses": action_statuses,
            "sourceWrite": False,
            "formalPublication": False,
            "policy": [
                "观察事实必须保留源系统、源表、源行和快照证据",
                "规则判断和派生事实必须记录输入事实、规则版本、解释和约束结果",
                "升级行动只生成本地计划，不直接写源系统或创建正式工单",
                "当前 91℃、DEF001、WO001 等示例不会在没有来源字段时被推断为真实事实",
            ],
        }
    finally:
        connection.close()


@domain_get("/api/semantic-facts")
def semantic_facts(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    fact_type: str = "all",
    status: str = "all",
    search: str | None = None,
) -> dict[str, Any]:
    connection = unified_semantics_connection()
    try:
        where: list[str] = []
        parameters: list[Any] = []
        if fact_type.strip() and fact_type != "all":
            where.append("fact_type=?")
            parameters.append(fact_type.strip())
        if status.strip() and status != "all":
            where.append("status=?")
            parameters.append(status.strip())
        if search and search.strip():
            value = f"%{search.strip()}%"
            where.append("(fact_id LIKE ? OR subject_key LIKE ? OR predicate LIKE ? OR source_schema LIKE ? OR source_table LIKE ? OR source_row_id LIKE ?)")
            parameters.extend([value] * 6)
        where_sql = f"WHERE {' AND '.join(where)}" if where else ""
        total = int(connection.execute(f"SELECT count(*) FROM semantic_fact {where_sql}", parameters).fetchone()[0])
        rows = connection.execute(
            f"""
            SELECT * FROM semantic_fact
            {where_sql}
            ORDER BY created_at DESC,fact_id
            LIMIT ? OFFSET ?
            """,
            parameters + [page_size, (page - 1) * page_size],
        ).fetchall()
        return {
            "items": [dict(row) for row in rows],
            "total": total,
            "page": page,
            "pageSize": page_size,
            "sourceWrite": False,
            "formalPublication": False,
        }
    finally:
        connection.close()


@domain_get("/api/semantic-facts/{fact_id}")
def semantic_fact_detail(fact_id: str) -> dict[str, Any]:
    connection = unified_semantics_connection()
    try:
        fact = connection.execute("SELECT * FROM semantic_fact WHERE fact_id=?", (fact_id,)).fetchone()
        if fact is None:
            raise HTTPException(status_code=404, detail="语义事实不存在")
        derivations = [dict(row) for row in connection.execute(
            "SELECT * FROM semantic_fact_derivation WHERE output_fact_id=? ORDER BY created_at DESC",
            (fact_id,),
        ).fetchall()]
        decisions = [dict(row) for row in connection.execute(
            "SELECT * FROM semantic_rule_decision WHERE input_fact_ids_json LIKE ? OR decision_id IN (SELECT decision_id FROM semantic_fact_derivation WHERE output_fact_id=?) ORDER BY created_at DESC",
            (f"%{fact_id}%", fact_id),
        ).fetchall()]
        decision_ids = [row["decision_id"] for row in decisions]
        actions: list[dict[str, Any]] = []
        if decision_ids:
            marks = ",".join("?" for _ in decision_ids)
            actions = [dict(row) for row in connection.execute(
                f"SELECT * FROM semantic_escalation_action WHERE decision_id IN ({marks}) ORDER BY created_at DESC",
                decision_ids,
            ).fetchall()]
        return {
            "fact": dict(fact),
            "derivations": derivations,
            "decisions": decisions,
            "actions": actions,
            "sourceWrite": False,
            "formalPublication": False,
        }
    finally:
        connection.close()


@domain_get("/api/semantic-status-dictionary/summary")
def semantic_status_dictionary_summary() -> dict[str, Any]:
    connection = unified_semantics_connection()
    try:
        latest = connection.execute(
            "SELECT * FROM semantic_status_dictionary_run ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        if latest is None:
            raise HTTPException(status_code=503, detail="缺陷状态字典没有构建批次")
        by_system = [dict(row) for row in connection.execute(
            "SELECT source_schema AS value,count(*) AS count FROM semantic_status_dictionary GROUP BY source_schema ORDER BY source_schema"
        ).fetchall()]
        by_status = [dict(row) for row in connection.execute(
            "SELECT mapping_status AS value,count(*) AS count FROM semantic_status_dictionary GROUP BY mapping_status ORDER BY mapping_status"
        ).fetchall()]
        canonical_states = [dict(row) for row in connection.execute(
            """
            SELECT canonical_state AS value,display_name,description,is_terminal,sort_order
            FROM semantic_canonical_state
            WHERE state_domain='DEFECT' AND status='active'
            ORDER BY sort_order
            """
        ).fetchall()]
        replay = connection.execute(
            "SELECT * FROM semantic_status_replay_run ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        return {
            "latestRun": dict(latest),
            "candidateCount": int(connection.execute("SELECT count(*) FROM semantic_status_dictionary").fetchone()[0]),
            "pendingCount": int(connection.execute("SELECT count(*) FROM semantic_status_dictionary WHERE mapping_status='pending'").fetchone()[0]),
            "approvedCount": int(connection.execute("SELECT count(*) FROM semantic_status_dictionary WHERE mapping_status='approved'").fetchone()[0]),
            "rejectedCount": int(connection.execute("SELECT count(*) FROM semantic_status_dictionary WHERE mapping_status='rejected'").fetchone()[0]),
            "evidenceRowCount": int(connection.execute("SELECT coalesce(sum(evidence_count),0) FROM semantic_status_dictionary").fetchone()[0]),
            "systems": by_system,
            "mappingStatuses": by_status,
            "canonicalStates": canonical_states,
            "latestReplay": dict(replay) if replay else None,
            "sourceWrite": False,
            "formalPublication": False,
            "policy": [
                "保留 HD/XNY 原始状态值，不跨系统自动合并或改写",
                "只有明确业务字典或责任人确认后，才映射到标准缺陷状态",
                "未知、空值和编码冲突保持 pending，不自动生成行动",
                "状态含义确认只写本地语义字典，不修改源表",
            ],
        }
    finally:
        connection.close()


@domain_get("/api/semantic-status-dictionary")
def semantic_status_dictionary(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    source_schema: str = "all",
    source_table: str = "all",
    mapping_status: str = "all",
    search: str | None = None,
) -> dict[str, Any]:
    connection = unified_semantics_connection()
    try:
        where: list[str] = []
        parameters: list[Any] = []
        if source_schema.strip() and source_schema != "all":
            where.append("source_schema=?")
            parameters.append(source_schema.strip())
        if source_table.strip() and source_table != "all":
            where.append("source_table=?")
            parameters.append(source_table.strip())
        if mapping_status.strip() and mapping_status != "all":
            where.append("mapping_status=?")
            parameters.append(mapping_status.strip())
        if search and search.strip():
            value = f"%{search.strip()}%"
            where.append("(status_id LIKE ? OR raw_status LIKE ? OR canonical_state LIKE ? OR business_meaning LIKE ? OR mapping_notes LIKE ?)")
            parameters.extend([value] * 5)
        where_sql = f"WHERE {' AND '.join(where)}" if where else ""
        total = int(connection.execute(f"SELECT count(*) FROM semantic_status_dictionary {where_sql}", parameters).fetchone()[0])
        rows = connection.execute(
            f"""
            SELECT * FROM semantic_status_dictionary
            {where_sql}
            ORDER BY mapping_status,source_schema,source_table,evidence_count DESC,status_id
            LIMIT ? OFFSET ?
            """,
            parameters + [page_size, (page - 1) * page_size],
        ).fetchall()
        return {
            "items": [dict(row) for row in rows],
            "total": total,
            "page": page,
            "pageSize": page_size,
            "sourceWrite": False,
            "formalPublication": False,
        }
    finally:
        connection.close()


@domain_get("/api/semantic-status-dictionary/{status_id}")
def semantic_status_dictionary_detail(status_id: str) -> dict[str, Any]:
    connection = unified_semantics_connection()
    try:
        row = connection.execute(
            "SELECT * FROM semantic_status_dictionary WHERE status_id=?", (status_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="缺陷状态字典条目不存在")
        item = dict(row)
        try:
            examples = json.loads(item.get("example_records_json") or "[]")
        except json.JSONDecodeError:
            examples = []
        item["examples"] = examples
        reviews = [dict(review) for review in connection.execute(
            "SELECT * FROM semantic_status_mapping_review WHERE status_id=? ORDER BY mapping_version DESC",
            (status_id,),
        ).fetchall()]
        return {"item": item, "reviews": reviews, "sourceWrite": False, "formalPublication": False}
    finally:
        connection.close()


def execute_status_mapping_replay_local() -> dict[str, Any]:
    script_path = SYSTEM_ROOT / "replay_defect_status_mapping.py"
    spec = importlib.util.spec_from_file_location("semantic_status_mapping_replay", script_path)
    if spec is None or spec.loader is None:
        raise HTTPException(status_code=503, detail="缺陷状态回放器不可用")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.replay(UNIFIED_SEMANTICS_DB)


@domain_post("/api/semantic-status-dictionary/replay")
def replay_semantic_status_dictionary() -> dict[str, Any]:
    result = execute_status_mapping_replay_local()
    return result


@domain_post("/api/semantic-status-dictionary/{status_id}/review")
def review_semantic_status_dictionary(status_id: str, request: DefectStatusReviewRequest, actor: str = Depends(require_decision_auth)) -> dict[str, Any]:
    connection = unified_semantics_write_connection()
    try:
        row = connection.execute(
            "SELECT * FROM semantic_status_dictionary WHERE status_id=?", (status_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="缺陷状态字典条目不存在")
        canonical_state = (request.canonical_state or "").strip().upper() or None
        canonical = None
        if canonical_state:
            canonical = connection.execute(
                """
                SELECT * FROM semantic_canonical_state
                WHERE state_domain='DEFECT' AND canonical_state=? AND status='active'
                """,
                (canonical_state,),
            ).fetchone()
        if request.decision == "approved" and canonical is None:
            raise HTTPException(status_code=400, detail="确认状态映射时必须选择有效的标准缺陷状态")
        meaning = (request.business_meaning or "").strip() or (canonical["display_name"] if canonical else None)
        reviewed_at = utc_now()
        mapping_version = int(row["mapping_version"] or 0) + 1
        review_id = f"SSMR-{hashlib.sha256(f'{status_id}|{mapping_version}|{reviewed_at}'.encode('utf-8')).hexdigest()[:24]}"
        connection.execute(
            """
            INSERT INTO semantic_status_mapping_review(
              review_id,status_id,mapping_version,previous_mapping_status,
              previous_canonical_state,decision,canonical_state,business_meaning,
              notes,reviewer,reviewed_at,source_write,formal_publication
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,0,0)
            """,
            (
                review_id,status_id,mapping_version,row["mapping_status"],row["canonical_state"],
                request.decision,canonical_state if request.decision == "approved" else None,
                meaning if request.decision == "approved" else None,request.notes.strip(),
                request.reviewer.strip() or actor,reviewed_at,
            ),
        )
        connection.execute(
            """
            UPDATE semantic_status_dictionary
            SET mapping_status=?,canonical_state=?,business_meaning=?,mapping_notes=?,
              mapping_version=?,reviewer=?,reviewed_at=?,updated_at=?
            WHERE status_id=?
            """,
            (
                request.decision,canonical_state if request.decision == "approved" else None,
                meaning if request.decision == "approved" else None,request.notes.strip(),mapping_version,
                request.reviewer.strip() or actor,reviewed_at,reviewed_at,status_id,
            ),
        )
        connection.commit()
        updated = connection.execute("SELECT * FROM semantic_status_dictionary WHERE status_id=?", (status_id,)).fetchone()
        return {"item": dict(updated), "reviewId": review_id, "sourceWrite": False, "formalPublication": False}
    finally:
        connection.close()


@domain_get("/api/semantic-states/summary")
def semantic_states_summary() -> dict[str, Any]:
    connection = unified_semantics_connection()
    try:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if "semantic_current_state" not in tables or "semantic_state_transition_run" not in tables:
            raise HTTPException(status_code=503, detail="状态迁移层尚未初始化")
        latest = connection.execute(
            "SELECT * FROM semantic_state_transition_run ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        by_state = [dict(row) for row in connection.execute(
            """
            SELECT current_state AS value,display_name,count(*) AS count
            FROM semantic_current_state
            WHERE status='current'
            GROUP BY current_state,display_name
            ORDER BY current_state
            """
        ).fetchall()]
        return {
            "latestRun": dict(latest) if latest else None,
            "currentStateCount": int(connection.execute("SELECT count(*) FROM semantic_current_state WHERE status='current'").fetchone()[0]),
            "transitionCount": int(connection.execute("SELECT count(*) FROM semantic_state_transition").fetchone()[0]),
            "reviewTransitionCount": int(connection.execute("SELECT count(*) FROM semantic_state_transition WHERE status='needs_review'").fetchone()[0]),
            "differenceCount": int(connection.execute("SELECT count(*) FROM semantic_state_replay_diff WHERE status IN ('reported','needs_review')").fetchone()[0]) if "semantic_state_replay_diff" in tables else 0,
            "states": by_state,
            "sourceWrite": False,
            "formalPublication": False,
            "policy": [
                "只消费已确认的 canonical_defect_state 事实",
                "无时间顺序证据的冲突状态不覆盖当前状态",
                "状态迁移和当前状态均属于本地语义覆盖层",
            ],
        }
    finally:
        connection.close()


@domain_post("/api/semantic-states/replay")
def replay_semantic_states(request: SemanticStateReplayRequest) -> dict[str, Any]:
    """Replay state transitions locally, optionally for one subject only."""
    executor_path = SYSTEM_ROOT / "execute_state_transitions.py"
    spec = importlib.util.spec_from_file_location("semantic_state_replay_runtime", executor_path)
    if spec is None or spec.loader is None:
        raise HTTPException(status_code=503, detail="状态回放执行器不可用")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.execute(UNIFIED_SEMANTICS_DB, request.subject_type, request.subject_key)


@domain_get("/api/semantic-states/replay-diffs")
def semantic_state_replay_diffs(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    run_id: str | None = None,
    difference_type: str | None = None,
) -> dict[str, Any]:
    connection = unified_semantics_connection()
    try:
        clauses: list[str] = []
        parameters: list[Any] = []
        if run_id:
            clauses.append("replay_run_id=?")
            parameters.append(run_id)
        if difference_type:
            clauses.append("difference_type=?")
            parameters.append(difference_type)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        total = int(connection.execute(f"SELECT count(*) FROM semantic_state_replay_diff {where}", parameters).fetchone()[0])
        rows = connection.execute(
            f"SELECT * FROM semantic_state_replay_diff {where} ORDER BY created_at DESC,diff_id LIMIT ? OFFSET ?",
            parameters + [page_size, (page - 1) * page_size],
        ).fetchall()
        return {"items": [dict(row) for row in rows], "total": total, "page": page, "pageSize": page_size, "sourceWrite": False, "formalPublication": False}
    finally:
        connection.close()


@domain_get("/api/semantic-events/summary")
def semantic_events_summary() -> dict[str, Any]:
    connection = unified_semantics_connection()
    try:
        latest = connection.execute(
            "SELECT * FROM semantic_event_layer_run ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        if latest is None:
            raise HTTPException(status_code=503, detail="统一事件层尚未构建")
        by_type = [dict(row) for row in connection.execute(
            "SELECT event_type AS value,count(*) AS count FROM semantic_event WHERE status IN ('observed','accepted') GROUP BY event_type ORDER BY event_type"
        ).fetchall()]
        by_source = [dict(row) for row in connection.execute(
            "SELECT source_schema AS value,count(*) AS count FROM semantic_event WHERE status IN ('observed','accepted') GROUP BY source_schema ORDER BY source_schema"
        ).fetchall()]
        return {
            "latestRun": dict(latest),
            "eventCount": int(connection.execute("SELECT count(*) FROM semantic_event WHERE status IN ('observed','accepted')").fetchone()[0]),
            "reviewEventCount": int(connection.execute("SELECT count(*) FROM semantic_event WHERE status='needs_review'").fetchone()[0]),
            "eventTypes": by_type,
            "sources": by_source,
            "sourceWrite": False,
            "formalPublication": False,
            "policy": [
                "只接收已有设备身份桥接且来源可追溯的事件事实",
                "保留源事件类型、源状态、源描述和发生时间",
                "事件层不替代事实层，也不直接产生行动",
            ],
        }
    finally:
        connection.close()


@domain_get("/api/semantic-events")
def semantic_events(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    event_type: str = "all",
    status: str = "all",
    site_id: str | None = None,
    search: str | None = None,
) -> dict[str, Any]:
    connection = unified_semantics_connection()
    try:
        where: list[str] = []
        parameters: list[Any] = []
        if event_type and event_type != "all":
            where.append("e.event_type=?")
            parameters.append(event_type)
        if status and status != "all":
            where.append("e.status=?")
            parameters.append(status)
        if site_id and site_id.strip():
            where.append("e.site_id=?")
            parameters.append(site_id.strip())
        if search and search.strip():
            value = f"%{search.strip()}%"
            where.append("(e.event_id LIKE ? OR e.subject_key LIKE ? OR e.source_row_id LIKE ? OR e.location_code LIKE ? OR e.raw_status LIKE ? OR e.description LIKE ?)")
            parameters.extend([value] * 6)
        where_sql = f"WHERE {' AND '.join(where)}" if where else ""
        total = int(connection.execute(f"SELECT count(*) FROM semantic_event e {where_sql}", parameters).fetchone()[0])
        rows = connection.execute(
            f"""
            SELECT e.*,s.current_state,s.display_name AS current_state_display
            FROM semantic_event e
            LEFT JOIN semantic_current_state s
              ON s.subject_type=e.subject_type AND s.subject_key=e.subject_key
             AND s.state_domain='DEFECT' AND s.status='current'
            {where_sql}
            ORDER BY e.occurred_at DESC,e.event_id
            LIMIT ? OFFSET ?
            """,
            parameters + [page_size, (page - 1) * page_size],
        ).fetchall()
        return {
            "items": [dict(row) for row in rows],
            "total": total,
            "page": page,
            "pageSize": page_size,
            "sourceWrite": False,
            "formalPublication": False,
        }
    finally:
        connection.close()


@domain_get("/api/semantic-events/{event_id}")
def semantic_event_detail(event_id: str) -> dict[str, Any]:
    connection = unified_semantics_connection()
    try:
        event = connection.execute("SELECT * FROM semantic_event WHERE event_id=?", (event_id,)).fetchone()
        if event is None:
            raise HTTPException(status_code=404, detail="统一事件不存在")
        item = dict(event)
        try:
            item["payload"] = json.loads(item.get("payload_json") or "{}")
        except json.JSONDecodeError:
            item["payload"] = {}
        source_facts = [dict(row) for row in connection.execute(
            """
            SELECT * FROM semantic_fact
            WHERE source_schema=? AND source_table=? AND source_row_id=? AND source_snapshot_id=?
            ORDER BY created_at DESC
            """,
            (event["source_schema"], event["source_table"], event["source_row_id"], event["source_snapshot_id"]),
        ).fetchall()]
        transitions = [dict(row) for row in connection.execute(
            "SELECT * FROM semantic_state_transition WHERE event_id=? ORDER BY created_at DESC",
            (event_id,),
        ).fetchall()]
        current = connection.execute(
            """
            SELECT * FROM semantic_current_state
            WHERE subject_type=? AND subject_key=? AND state_domain='DEFECT' AND status='current'
            """,
            (event["subject_type"], event["subject_key"]),
        ).fetchone()
        return {
            "event": item,
            "sourceFacts": source_facts,
            "transitions": transitions,
            "currentState": dict(current) if current else None,
            "sourceWrite": False,
            "formalPublication": False,
        }
    finally:
        connection.close()


@domain_get("/api/semantic-execution/summary")
def semantic_execution_summary() -> dict[str, Any]:
    connection = unified_semantics_connection()
    try:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if "semantic_action_run" not in tables:
            return {"runCount": 0, "statuses": [], "latest": None, "adapterCount": 0, "ledgerCount": 0, "sourceWrite": False, "formalPublication": False}
        statuses = [dict(row) for row in connection.execute(
            "SELECT status AS value,count(*) AS count FROM semantic_action_run GROUP BY status ORDER BY status"
        ).fetchall()]
        latest = connection.execute(
            "SELECT run_id,asset_id,asset_version_id,action_id,mode,status,target_count,created_at FROM semantic_action_run ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        return {
            "runCount": int(connection.execute("SELECT count(*) FROM semantic_action_run").fetchone()[0]),
            "statuses": statuses,
            "latest": dict(latest) if latest else None,
            "adapterCount": int(connection.execute("SELECT count(*) FROM semantic_execution_adapter").fetchone()[0]) if "semantic_execution_adapter" in tables else 0,
            "ledgerCount": int(connection.execute("SELECT count(*) FROM semantic_execution_ledger").fetchone()[0]) if "semantic_execution_ledger" in tables else 0,
            "sourceWrite": False,
            "formalPublication": False,
        }
    finally:
        connection.close()


@domain_post("/api/semantic-execution/dispatch")
def semantic_execution_dispatch(request: SemanticExecutionDispatchRequest) -> dict[str, Any]:
    """Create a governed execution-ledger entry; never calls a source system."""
    connection = unified_semantics_write_connection()
    try:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        required = {"semantic_execution_adapter", "semantic_execution_ledger"}
        if not required.issubset(tables):
            raise HTTPException(status_code=503, detail="执行账本尚未初始化，请先运行执行层初始化")
        existing = connection.execute("SELECT * FROM semantic_execution_ledger WHERE idempotency_key=?", (request.idempotency_key,)).fetchone()
        if existing:
            return {"execution": dict(existing), "idempotentReplay": True, "sourceWrite": False, "formalPublication": False}
        adapter = connection.execute("SELECT * FROM semantic_execution_adapter WHERE adapter_id=? AND status='active'", (request.adapter_id,)).fetchone()
        if adapter is None:
            raise HTTPException(status_code=404, detail="执行适配器不存在或未启用")
        plan = connection.execute("SELECT * FROM semantic_action_plan WHERE plan_id=?", (request.action_plan_id,)).fetchone() if request.action_plan_id else None
        if plan is not None and plan["requires_approval"] and plan["status"] != "APPROVED":
            raise HTTPException(status_code=409, detail="行动计划尚未完成独立审批，不能进入执行台账")
        execution_id = f"EXEC-{hashlib.sha256(request.idempotency_key.encode('utf-8')).hexdigest()[:24]}"
        created = utc_now()
        response = {"mode": adapter["mode"], "adapter": adapter["adapter_id"], "actionRunId": request.action_run_id, "actionPlanId": request.action_plan_id, "source_write": False, "formal_publication": False, "note": "当前适配器仅生成本地执行台账，不调用外部系统"}
        connection.execute(
            """INSERT INTO semantic_execution_ledger(
              execution_id,action_run_id,action_plan_id,approval_receipt,adapter_id,
              idempotency_key,status,external_execution_ref,request_json,response_json,
              error_code,error_message,source_write,formal_publication,created_at,updated_at,completed_at
            ) VALUES (?,?,?,?,?,?, 'planned',NULL,?,?,?,?,0,0,?,?,NULL)""",
            (execution_id, request.action_run_id, request.action_plan_id, request.approval_receipt, request.adapter_id, request.idempotency_key, json.dumps(request.request_payload, ensure_ascii=False), json.dumps(response, ensure_ascii=False), None, None, created, created),
        )
        connection.commit()
        execution = connection.execute("SELECT * FROM semantic_execution_ledger WHERE execution_id=?", (execution_id,)).fetchone()
        return {"execution": dict(execution), "idempotentReplay": False, "sourceWrite": False, "formalPublication": False}
    finally:
        connection.close()


@domain_post("/api/semantic-execution/preview")
def semantic_execution_preview(request: SemanticExecutionPreviewRequest) -> dict[str, Any]:
    connection = unified_semantics_write_connection()
    try:
        existing = connection.execute(
            "SELECT * FROM semantic_action_run WHERE idempotency_key=?",
            (request.idempotency_key,),
        ).fetchone()
        if existing:
            return {
                "runId": existing["run_id"],
                "assetId": existing["asset_id"],
                "assetVersionId": existing["asset_version_id"],
                "mode": existing["mode"],
                "status": existing["status"],
                "targetCount": existing["target_count"],
                "gates": json.loads(existing["gate_summary_json"]),
                "actionPlan": json.loads(existing["action_plan_json"]),
                "idempotentReplay": True,
                "sourceWrite": False,
                "formalPublication": False,
            }
        asset = connection.execute("SELECT * FROM knowledge_asset WHERE asset_id=?", (request.asset_id,)).fetchone()
        if asset is None:
            raise HTTPException(status_code=404, detail="知识资产不存在")
        version = None
        if request.asset_version_id:
            version = connection.execute(
                "SELECT * FROM knowledge_asset_version WHERE asset_version_id=? AND asset_id=?",
                (request.asset_version_id, request.asset_id),
            ).fetchone()
        if version is None:
            version = connection.execute(
                "SELECT * FROM knowledge_asset_version WHERE asset_id=? AND version=?",
                (request.asset_id, asset["current_version"]),
            ).fetchone()
        if version is None:
            version = connection.execute(
                "SELECT * FROM knowledge_asset_version WHERE asset_id=? ORDER BY created_at DESC LIMIT 1",
                (request.asset_id,),
            ).fetchone()
        if version is None:
            raise HTTPException(status_code=409, detail="知识资产没有可执行版本")
        contract = connection.execute(
            "SELECT * FROM machine_semantic_contract WHERE asset_version_id=?",
            (version["asset_version_id"],),
        ).fetchone()
        action = connection.execute(
            "SELECT * FROM machine_action_spec WHERE asset_version_id=? ORDER BY created_at DESC LIMIT 1",
            (version["asset_version_id"],),
        ).fetchone()
        constraints = connection.execute(
            "SELECT * FROM machine_constraint_spec WHERE asset_version_id=? ORDER BY constraint_key",
            (version["asset_version_id"],),
        ).fetchall()
        if contract is None or action is None:
            raise HTTPException(status_code=409, detail="机器语义契约不完整，不能生成执行预演")

        gates: list[dict[str, Any]] = []
        gates.append({"key": "contract_exists", "status": "pass", "severity": "high", "message": "判断、计算、约束和行动契约存在"})
        gates.append({"key": "source_write_forbidden", "status": "pass", "severity": "critical", "message": "本次预演 source_write=0，formal_publication=0"})
        if action["action_type"] == "replay_evaluate":
            gates.append({"key": "replay_action", "status": "pass", "severity": "high", "message": "当前行动是回放评估，不执行正式发布"})
        elif int(version["replay_count"] or 0) > 0 and int(version["replay_fail_count"] or 0) == 0:
            gates.append({"key": "replay_before_enable", "status": "pass", "severity": "high", "message": f"回放 {version['replay_count']} 条，失败 0 条"})
        else:
            gates.append({"key": "replay_before_enable", "status": "needs_review", "severity": "high", "message": "当前版本尚无通过的完整回放证据"})
        if int(action["requires_approval"] or 0):
            approval_status = "pass" if asset["status"] in {"approved", "enabled"} else "needs_review"
            gates.append({"key": "approval_before_action", "status": approval_status, "severity": "high", "message": "行动需要独立人工审批" if approval_status != "pass" else "资产已具备审批/启用状态"})
        else:
            gates.append({"key": "approval_before_action", "status": "pass", "severity": "low", "message": "当前回放行动不产生正式发布"})

        hard_blocks = [gate for gate in gates if gate["status"] == "blocked"]
        reviews = [gate for gate in gates if gate["status"] == "needs_review"]
        status = "blocked" if hard_blocks else ("needs_review" if reviews else "ready")
        target_count = min(int(version["preview_count"] or version["replay_count"] or 0), request.sample_size)
        run_id = f"SAR-{hashlib.sha256(request.idempotency_key.encode('utf-8')).hexdigest()[:24]}"
        action_plan = {
            "assetKey": asset["asset_key"],
            "assetType": asset["asset_type"],
            "assetVersion": version["version"],
            "actionType": action["action_type"],
            "targetType": action["target_type"],
            "targetScope": request.target_scope,
            "sampleSize": request.sample_size,
            "requiresApproval": bool(action["requires_approval"]),
            "idempotencyKeyTemplate": action["idempotency_key_template"],
            "note": request.note,
        }
        connection.execute(
            """
            INSERT INTO semantic_action_run(run_id,idempotency_key,asset_id,asset_version_id,action_id,mode,status,
              target_scope,target_count,gate_summary_json,action_plan_json,source_write,formal_publication,created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,0,0,?)
            """,
            (run_id, request.idempotency_key, request.asset_id, version["asset_version_id"], action["action_id"], "preview",
             status, request.target_scope, target_count, json.dumps(gates, ensure_ascii=False),
             json.dumps(action_plan, ensure_ascii=False), utc_now()),
        )
        connection.commit()
        return {
            "runId": run_id,
            "assetId": request.asset_id,
            "assetVersionId": version["asset_version_id"],
            "mode": "preview",
            "status": status,
            "targetCount": target_count,
            "gates": gates,
            "actionPlan": action_plan,
            "idempotentReplay": False,
            "sourceWrite": False,
            "formalPublication": False,
        }
    finally:
        connection.close()


def decision_layer_tables(connection: sqlite3.Connection) -> set[str]:
    return {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}


@domain_get("/api/semantic-decisions/summary")
def semantic_decisions_summary() -> dict[str, Any]:
    """Summarize the local Decision -> ActionPlan -> Approval boundary."""
    connection = unified_semantics_connection()
    try:
        tables = decision_layer_tables(connection)
        required = {"semantic_action_plan", "semantic_action_approval", "semantic_decision_layer_run"}
        if not required.issubset(tables):
            raise HTTPException(status_code=503, detail="决策行动层尚未初始化，请先运行决策层构建脚本")
        latest = connection.execute("SELECT * FROM semantic_decision_layer_run ORDER BY created_at DESC LIMIT 1").fetchone()
        if latest is None:
            raise HTTPException(status_code=503, detail="决策行动层没有构建批次")
        decision_statuses = [dict(row) for row in connection.execute(
            "SELECT status AS value,count(*) AS count FROM semantic_rule_decision GROUP BY status ORDER BY status"
        ).fetchall()]
        plan_statuses = [dict(row) for row in connection.execute(
            "SELECT status AS value,count(*) AS count FROM semantic_action_plan GROUP BY status ORDER BY status"
        ).fetchall()]
        approval_statuses = [dict(row) for row in connection.execute(
            "SELECT status AS value,count(*) AS count FROM semantic_action_approval GROUP BY status ORDER BY status"
        ).fetchall()]
        return {
            "latestRun": dict(latest),
            "decisionCount": int(connection.execute("SELECT count(*) FROM semantic_rule_decision WHERE status IN ('accepted','proposed','needs_review')").fetchone()[0]),
            "actionRuleCount": int(connection.execute("SELECT count(*) FROM semantic_action_rule WHERE status='enabled'").fetchone()[0]) if "semantic_action_rule" in tables else 0,
            "actionRuleMatchCount": int((latest["action_rule_match_count"] if latest and "action_rule_match_count" in latest.keys() else 0) or 0),
            "riskEvidenceCount": int((latest["risk_evidence_count"] if latest and "risk_evidence_count" in latest.keys() else 0) or 0),
            "actionPlanCount": int(connection.execute("SELECT count(*) FROM semantic_action_plan").fetchone()[0]),
            "pendingApprovalCount": int(connection.execute("SELECT count(*) FROM semantic_action_plan WHERE status='PENDING_APPROVAL'").fetchone()[0]),
            "approvalCount": int(connection.execute("SELECT count(*) FROM semantic_action_approval").fetchone()[0]),
            "decisionStatuses": decision_statuses,
            "planStatuses": plan_statuses,
            "approvalStatuses": approval_statuses,
            "sourceWrite": False,
            "formalPublication": False,
            "policy": [
                "只消费已接受或待复核的规则判断，不从自然语言直接生成行动",
                "没有明确 semantic_escalation_action 的 requires_action 判断不会被猜测成工单",
                "ActionPlan 只能写本地语义覆盖层，审批不会执行源系统写入",
                "审批与执行分离；当前批准只形成本地审批凭据，不调用源系统",
            ],
        }
    finally:
        connection.close()


@domain_get("/api/semantic-decisions")
def semantic_decisions(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    status: str = "all",
    requires_action: str = "all",
    search: str | None = None,
) -> dict[str, Any]:
    connection = unified_semantics_connection()
    try:
        if "semantic_action_plan" not in decision_layer_tables(connection):
            raise HTTPException(status_code=503, detail="决策行动层尚未初始化")
        where: list[str] = []
        parameters: list[Any] = []
        if status and status != "all":
            where.append("d.status=?")
            parameters.append(status)
        if requires_action in {"0", "1"}:
            where.append("d.requires_action=?")
            parameters.append(int(requires_action))
        if search and search.strip():
            term = f"%{search.strip()}%"
            where.append("(d.decision_id LIKE ? OR d.subject_key LIKE ? OR d.decision LIKE ? OR d.rule_asset_id LIKE ? OR d.explanation LIKE ?)")
            parameters.extend([term] * 5)
        where_sql = f"WHERE {' AND '.join(where)}" if where else ""
        total = int(connection.execute(f"SELECT count(*) FROM semantic_rule_decision d {where_sql}", parameters).fetchone()[0])
        rows = connection.execute(
            f"""
            SELECT d.*,p.plan_id,p.action_type,p.status AS plan_status,p.requires_approval AS plan_requires_approval
            FROM semantic_rule_decision d
            LEFT JOIN semantic_action_plan p ON p.decision_id=d.decision_id
            {where_sql}
            ORDER BY d.created_at DESC,d.decision_id
            LIMIT ? OFFSET ?
            """,
            parameters + [page_size, (page - 1) * page_size],
        ).fetchall()
        return {"items": [dict(row) for row in rows], "total": total, "page": page, "pageSize": page_size, "sourceWrite": False, "formalPublication": False}
    finally:
        connection.close()


@domain_get("/api/semantic-action-plans")
def semantic_action_plans(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    status: str = "all",
    search: str | None = None,
) -> dict[str, Any]:
    connection = unified_semantics_connection()
    try:
        if "semantic_action_plan" not in decision_layer_tables(connection):
            raise HTTPException(status_code=503, detail="决策行动层尚未初始化")
        where: list[str] = []
        parameters: list[Any] = []
        if status and status != "all":
            where.append("p.status=?")
            parameters.append(status)
        if search and search.strip():
            term = f"%{search.strip()}%"
            where.append("(p.plan_id LIKE ? OR p.decision_id LIKE ? OR p.action_type LIKE ? OR p.target_key LIKE ? OR p.reason LIKE ?)")
            parameters.extend([term] * 5)
        where_sql = f"WHERE {' AND '.join(where)}" if where else ""
        total = int(connection.execute(f"SELECT count(*) FROM semantic_action_plan p {where_sql}", parameters).fetchone()[0])
        rows = connection.execute(
            f"""
            SELECT p.*,a.status AS approval_status,a.reviewer,a.comment,a.approval_receipt
            FROM semantic_action_plan p
            LEFT JOIN semantic_action_approval a ON a.plan_id=p.plan_id
            {where_sql}
            ORDER BY p.updated_at DESC,p.plan_id
            LIMIT ? OFFSET ?
            """,
            parameters + [page_size, (page - 1) * page_size],
        ).fetchall()
        return {"items": [dict(row) for row in rows], "total": total, "page": page, "pageSize": page_size, "sourceWrite": False, "formalPublication": False}
    finally:
        connection.close()


@domain_get("/api/semantic-action-plans/summary")
def semantic_action_plans_summary() -> dict[str, Any]:
    """Provide the same governed count for callers focused on action plans."""
    return semantic_decisions_summary()


@domain_get("/api/semantic-action-plans/{plan_id}")
def semantic_action_plan_detail(plan_id: str) -> dict[str, Any]:
    connection = unified_semantics_connection()
    try:
        plan = connection.execute("SELECT * FROM semantic_action_plan WHERE plan_id=?", (plan_id,)).fetchone()
        if plan is None:
            raise HTTPException(status_code=404, detail="行动计划不存在")
        decision = connection.execute("SELECT * FROM semantic_rule_decision WHERE decision_id=?", (plan["decision_id"],)).fetchone()
        approval = connection.execute("SELECT * FROM semantic_action_approval WHERE plan_id=?", (plan_id,)).fetchone()
        payload = dict(plan)
        try:
            payload["payload"] = json.loads(payload.pop("payload_json") or "{}")
        except json.JSONDecodeError:
            payload["payload"] = {}
        return {"plan": payload, "decision": dict(decision) if decision else None, "approval": dict(approval) if approval else None, "sourceWrite": False, "formalPublication": False}
    finally:
        connection.close()


@domain_post("/api/semantic-action-plans/{plan_id}/approval")
def review_semantic_action_plan(plan_id: str, request: SemanticActionApprovalRequest, actor: str = Depends(require_decision_auth)) -> dict[str, Any]:
    """Record local approval only; never execute the planned action."""
    connection = unified_semantics_write_connection()
    try:
        plan = connection.execute("SELECT * FROM semantic_action_plan WHERE plan_id=?", (plan_id,)).fetchone()
        if plan is None:
            raise HTTPException(status_code=404, detail="行动计划不存在")
        if plan["status"] != "PENDING_APPROVAL":
            raise HTTPException(status_code=409, detail=f"当前行动计划状态为 {plan['status']}，不能重复审批")
        approval = connection.execute("SELECT * FROM semantic_action_approval WHERE plan_id=?", (plan_id,)).fetchone()
        if approval is None:
            raise HTTPException(status_code=409, detail="行动计划缺少审批任务，不能审批")
        reviewed_at = utc_now()
        receipt = f"semantic-action-approval-{uuid.uuid4().hex}"
        final_status = "APPROVED" if request.decision == "approved" else "REJECTED"
        connection.execute(
            "UPDATE semantic_action_approval SET status=?,reviewer=?,comment=?,approval_receipt=?,reviewed_at=? WHERE plan_id=?",
            (final_status, request.reviewer.strip() or actor, request.comment.strip(), receipt, reviewed_at, plan_id),
        )
        connection.execute("UPDATE semantic_action_plan SET status=?,updated_at=? WHERE plan_id=?", (final_status, reviewed_at, plan_id))
        connection.commit()
        return {"planId": plan_id, "status": final_status, "approvalReceipt": receipt, "sourceWrite": False, "formalPublication": False, "note": "审批仅写本地审批记录，未执行行动、未写源系统"}
    finally:
        connection.close()


def ontology_meta_tables(connection: sqlite3.Connection) -> set[str]:
    return {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}


@domain_get("/api/ontology/meta-summary")
def ontology_meta_summary() -> dict[str, Any]:
    """Expose the versioned meta-model and its validation gaps to UI/Agents."""
    connection = unified_semantics_connection()
    try:
        tables = ontology_meta_tables(connection)
        required = {
            "ontology_object_type", "ontology_property_type", "ontology_relation_type",
            "ontology_event_type", "ontology_state_machine", "ontology_transition_rule",
            "ontology_meta_model_run",
        }
        if not required.issubset(tables):
            raise HTTPException(status_code=503, detail="本体元模型尚未初始化，请先运行 build_ontology_meta_model.py")
        latest = connection.execute("SELECT * FROM ontology_meta_model_run ORDER BY created_at DESC LIMIT 1").fetchone()
        core_keys = [
            "device", "site", "center", "specialty", "team", "inspection", "abnormal_inspection",
            "defect", "repeated_defect", "severe_defect", "defect_resolution", "work_order", "work_permit", "human_review",
        ]
        marks = ",".join("?" for _ in core_keys)
        core_rows = connection.execute(
            f"SELECT object_type,display_name,kind,version,review_status,status FROM ontology_object_type WHERE object_type IN ({marks}) ORDER BY object_type",
            core_keys,
        ).fetchall()
        core_by_key = {row["object_type"]: dict(row) for row in core_rows}
        missing_core = [key for key in core_keys if key not in core_by_key]
        relation_statuses = [dict(row) for row in connection.execute(
            "SELECT review_status AS value,count(*) AS count FROM ontology_relation_type WHERE status='active' GROUP BY review_status ORDER BY review_status"
        ).fetchall()]
        event_statuses = [dict(row) for row in connection.execute(
            "SELECT review_status AS value,count(*) AS count FROM ontology_event_type WHERE status='active' GROUP BY review_status ORDER BY review_status"
        ).fetchall()]
        unregistered_relations = [dict(row) for row in connection.execute(
            """SELECT r.predicate, min(r.subject_type) AS subject_type, min(r.object_type) AS object_type
               FROM business_object_relation r
               LEFT JOIN ontology_relation_type t ON t.predicate=r.predicate
               WHERE r.status='accepted' AND (t.predicate IS NULL OR t.review_status='needs_review')
               GROUP BY r.predicate ORDER BY r.predicate"""
        ).fetchall()]
        unregistered_events = [dict(row) for row in connection.execute(
            """SELECT DISTINCT e.event_type, e.subject_type
               FROM semantic_event e
               LEFT JOIN ontology_event_type t ON t.event_type=e.event_type
               WHERE e.status IN ('observed','accepted') AND (t.event_type IS NULL OR t.review_status='needs_review')
               ORDER BY e.event_type"""
        ).fetchall()]
        return {
            "schemaVersion": "ontology-runtime-v1",
            "latestRun": dict(latest) if latest else None,
            "objectTypeCount": int(connection.execute("SELECT count(*) FROM ontology_object_type WHERE status='active'").fetchone()[0]),
            "propertyTypeCount": int(connection.execute("SELECT count(*) FROM ontology_property_type WHERE status='active'").fetchone()[0]),
            "relationTypeCount": int(connection.execute("SELECT count(*) FROM ontology_relation_type WHERE status='active'").fetchone()[0]),
            "eventTypeCount": int(connection.execute("SELECT count(*) FROM ontology_event_type WHERE status='active'").fetchone()[0]),
            "stateMachineCount": int(connection.execute("SELECT count(*) FROM ontology_state_machine WHERE status='active'").fetchone()[0]),
            "transitionRuleCount": int(connection.execute("SELECT count(*) FROM ontology_transition_rule WHERE status='active'").fetchone()[0]),
            "coreObjects": [core_by_key[key] for key in core_keys if key in core_by_key],
            "missingCoreObjects": missing_core,
            "relationReviewStatuses": relation_statuses,
            "eventReviewStatuses": event_statuses,
            "unregisteredRelations": unregistered_relations,
            "unregisteredEvents": unregistered_events,
            "sourceWrite": False,
            "formalPublication": False,
            "policy": [
                "对象、属性、关系、事件和状态机先注册，再允许运行层消费",
                "未知 predicate/event_type 不静默删除，先标记 needs_review",
                "元模型只写本地 SQLite 语义覆盖层，不修改源表",
            ],
        }
    finally:
        connection.close()


@domain_get("/api/ontology/object-types")
def ontology_object_types(kind: str = "all", review_status: str = "all") -> dict[str, Any]:
    connection = unified_semantics_connection()
    try:
        where: list[str] = ["status='active'"]
        parameters: list[Any] = []
        if kind and kind != "all":
            where.append("kind=?")
            parameters.append(kind)
        if review_status and review_status != "all":
            where.append("review_status=?")
            parameters.append(review_status)
        rows = connection.execute(
            f"SELECT * FROM ontology_object_type WHERE {' AND '.join(where)} ORDER BY kind,object_type",
            parameters,
        ).fetchall()
        return {"schemaVersion": "ontology-runtime-v1", "items": [dict(row) for row in rows], "sourceWrite": False, "formalPublication": False}
    finally:
        connection.close()


@domain_get("/api/ontology/relation-types")
def ontology_relation_types(review_status: str = "all") -> dict[str, Any]:
    connection = unified_semantics_connection()
    try:
        where = ["status='active'"]
        parameters: list[Any] = []
        if review_status and review_status != "all":
            where.append("review_status=?")
            parameters.append(review_status)
        rows = connection.execute(
            f"SELECT * FROM ontology_relation_type WHERE {' AND '.join(where)} ORDER BY review_status,predicate",
            parameters,
        ).fetchall()
        return {"schemaVersion": "ontology-runtime-v1", "items": [dict(row) for row in rows], "sourceWrite": False, "formalPublication": False}
    finally:
        connection.close()


@domain_get("/api/ontology/event-types")
def ontology_event_types(review_status: str = "all") -> dict[str, Any]:
    connection = unified_semantics_connection()
    try:
        where = ["status='active'"]
        parameters: list[Any] = []
        if review_status and review_status != "all":
            where.append("review_status=?")
            parameters.append(review_status)
        rows = connection.execute(
            f"SELECT * FROM ontology_event_type WHERE {' AND '.join(where)} ORDER BY review_status,event_type",
            parameters,
        ).fetchall()
        return {"schemaVersion": "ontology-runtime-v1", "items": [dict(row) for row in rows], "sourceWrite": False, "formalPublication": False}
    finally:
        connection.close()


def world_model_device_context_payload(unified_device_id: str, as_of: str | None = None) -> dict[str, Any]:
    identity = identity_result_connection()
    overlay = unified_semantics_connection()
    canonical_connection = canonical_semantics_connection()
    try:
        device = identity.execute("SELECT * FROM unified_device WHERE unified_device_id=?", (unified_device_id,)).fetchone()
        if device is None:
            raise HTTPException(status_code=404, detail="统一设备对象不存在")
        canonical_context = CanonicalReader(canonical_connection).device_context(unified_device_id, as_of)
        mappings = [dict(row) for row in identity.execute(
            """SELECT source_schema,source_table_group,source_table,source_row_id,source_key_type,source_key,
               raw_description,location_code,match_method,match_confidence,status,evidence_json,source_snapshot_id
               FROM device_identity_map WHERE unified_device_id=? ORDER BY source_schema,source_table,source_row_id""",
            (unified_device_id,),
        ).fetchall()]
        # Canonical RDF is the read authority wherever the object has already
        # entered the completed projection. Until the migration coverage gate
        # reaches 100%, an unprojected object is served by the read-only
        # relational compatibility path and is explicitly labelled pending.
        if canonical_context is not None:
            relation_rows = canonical_context["relations"]
            recent_events = canonical_context["events"]
            facts = canonical_context["facts"]
            current_states = canonical_context["states"]
        else:
            relation_rows = [dict(row) for row in overlay.execute(
                """SELECT relation_id,subject_type,subject_key,predicate,object_type,object_key,source_schema,source_table,
                   source_row_id,status,confidence,evidence_json,source_snapshot_id
                   FROM business_object_relation WHERE (subject_type='device' AND subject_key=?)
                      OR (object_type='device' AND object_key=?) ORDER BY predicate,relation_id""",
                (unified_device_id, unified_device_id),
            ).fetchall()]
            event_where = "subject_key=?"
            event_params: list[Any] = [unified_device_id]
            if as_of:
                event_where += " AND (occurred_at IS NULL OR occurred_at<=?)"
                event_params.append(as_of)
            recent_events = [dict(row) for row in overlay.execute(
                f"SELECT * FROM semantic_event WHERE {event_where} AND status IN ('observed','accepted') ORDER BY occurred_at DESC,event_id LIMIT 200",
                event_params,
            ).fetchall()]
            fact_where = "subject_key=? AND status<>'retracted'"
            fact_params: list[Any] = [unified_device_id]
            if as_of:
                fact_where += " AND (observed_at IS NULL OR observed_at<=?)"
                fact_params.append(as_of)
            facts = [dict(row) for row in overlay.execute(
                f"SELECT * FROM semantic_fact WHERE {fact_where} ORDER BY created_at DESC,fact_id LIMIT 500",
                fact_params,
            ).fetchall()]
            current_states = [dict(row) for row in overlay.execute(
                "SELECT * FROM semantic_current_state WHERE subject_key=? AND status='current' ORDER BY state_domain",
                (unified_device_id,),
            ).fetchall()]
        decisions = [dict(row) for row in overlay.execute(
            "SELECT * FROM semantic_rule_decision WHERE subject_key=? AND status IN ('accepted','proposed','needs_review') ORDER BY created_at DESC LIMIT 200",
            (unified_device_id,),
        ).fetchall()]
        decision_ids = [row["decision_id"] for row in decisions]
        actions: list[dict[str, Any]] = []
        if decision_ids and "semantic_action_plan" in ontology_meta_tables(overlay):
            marks = ",".join("?" for _ in decision_ids)
            actions = [dict(row) for row in overlay.execute(
                f"SELECT * FROM semantic_action_plan WHERE decision_id IN ({marks}) ORDER BY updated_at DESC",
                decision_ids,
            ).fetchall()]
        rules = [dict(row) for row in overlay.execute(
            "SELECT * FROM semantic_logic_rule WHERE status='enabled' ORDER BY rule_asset_id"
        ).fetchall()]
        if "semantic_action_rule" in ontology_meta_tables(overlay):
            rules.extend(dict(row) for row in overlay.execute(
                """SELECT rule_id AS rule_asset_id,rule_version AS rule_version_id,title AS decision,action_type AS output_fact_type,
                   risk_level,requires_approval,status,source_write,formal_publication
                   FROM semantic_action_rule WHERE status='enabled' ORDER BY rule_id"""
            ).fetchall())
        observed = sum(1 for row in facts if row["status"] == "observed")
        derived = sum(1 for row in facts if row["status"] in {"derived", "accepted"})
        object_payload = unified_device_list_row(
            device,
            len(mappings),
            len(mappings),
            len(relation_rows),
            len(mappings),
            0,
        )
        return {
            "schemaVersion": "world-context-v1",
            "semanticSourceOfTruth": "canonical-rdf-dataset" if canonical_context is not None else "relational-runtime-pending-canonical",
            "canonicalRunId": canonical_context["runId"] if canonical_context is not None else None,
            "canonicalSubjectIri": canonical_context["subjectIri"] if canonical_context is not None else None,
            "object": object_payload,
            "identity": {"sourceIdentityKey": device["source_identity_key"], "mappings": mappings},
            "organization": {"siteId": device["site_id"], "orgId": device["org_id"] or "", "classificationId": device["classification_id"] or ""},
            "relations": relation_rows,
            "currentStates": current_states,
            "recentEvents": recent_events,
            "observedFacts": [row for row in facts if row["status"] == "observed"],
            "derivedFacts": [row for row in facts if row["status"] in {"derived", "accepted"}],
            "applicableRules": rules,
            "decisions": decisions,
            "pendingActions": actions,
            "evidenceSummary": {"identityMappings": len(mappings), "relationCount": len(relation_rows), "eventCount": len(recent_events), "observedFactCount": observed, "derivedFactCount": derived, "canonicalProvenance": canonical_context is not None},
            "asOf": as_of or utc_now(),
            "traceId": ontology_trace_id("WORLD", unified_device_id),
            "sourceWrite": False,
            "formalPublication": False,
        }
    finally:
        canonical_connection.close()
        overlay.close()
        identity.close()


@domain_get("/api/world-model/device/{unified_device_id}/context")
def world_model_device_context(unified_device_id: str, as_of: str | None = None) -> dict[str, Any]:
    return world_model_device_context_payload(unified_device_id, as_of)


@domain_get("/api/world-model/device/{unified_device_id}/timeline")
def world_model_device_timeline(
    unified_device_id: str,
    from_time: str | None = Query(default=None, alias="from"),
    to_time: str | None = Query(default=None, alias="to"),
    event_type: str = "all",
) -> dict[str, Any]:
    context = world_model_device_context_payload(unified_device_id, to_time)
    events = context["recentEvents"]
    if event_type != "all":
        events = [row for row in events if row.get("event_type") == event_type]
    if from_time:
        events = [row for row in events if not row.get("occurred_at") or row.get("occurred_at") >= from_time]
    transitions: list[dict[str, Any]] = []
    connection = unified_semantics_connection()
    try:
        transitions = [dict(row) for row in connection.execute(
            "SELECT * FROM semantic_state_transition WHERE subject_key=? ORDER BY effective_at DESC,created_at DESC",
            (unified_device_id,),
        ).fetchall()]
    finally:
        connection.close()
    timeline = [
        {"kind": "event", "at": row.get("occurred_at") or row.get("recorded_at"), "id": row.get("event_id"), "payload": row}
        for row in events
    ] + [
        {"kind": "state_transition", "at": row.get("effective_at") or row.get("created_at"), "id": row.get("transition_id"), "payload": row}
        for row in transitions
    ]
    timeline.sort(key=lambda row: (row.get("at") or "", row.get("id") or ""), reverse=True)
    return {"schemaVersion": "world-timeline-v1", "unifiedDeviceId": unified_device_id, "items": timeline, "currentStates": context["currentStates"], "traceId": ontology_trace_id("TIMELINE", unified_device_id), "sourceWrite": False, "formalPublication": False}


@domain_get("/api/world-model/facts/{fact_id}/explain")
def world_model_fact_explain(fact_id: str) -> dict[str, Any]:
    connection = unified_semantics_connection()
    try:
        fact = connection.execute("SELECT * FROM semantic_fact WHERE fact_id=?", (fact_id,)).fetchone()
        if fact is None:
            raise HTTPException(status_code=404, detail="语义事实不存在")
        derivations = [dict(row) for row in connection.execute(
            "SELECT * FROM semantic_fact_derivation WHERE output_fact_id=? ORDER BY created_at DESC",
            (fact_id,),
        ).fetchall()]
        input_ids: list[str] = []
        for row in derivations:
            input_ids.extend(parse_json_array(row["input_fact_ids_json"]))
        input_ids = list(dict.fromkeys(input_ids))
        input_facts: list[dict[str, Any]] = []
        if input_ids:
            marks = ",".join("?" for _ in input_ids)
            input_facts = [dict(row) for row in connection.execute(f"SELECT * FROM semantic_fact WHERE fact_id IN ({marks})", input_ids).fetchall()]
        decisions = [dict(row) for row in connection.execute(
            "SELECT * FROM semantic_rule_decision WHERE decision_id IN (SELECT decision_id FROM semantic_fact_derivation WHERE output_fact_id=?) OR input_fact_ids_json LIKE ? ORDER BY created_at DESC",
            (fact_id, f"%{fact_id}%"),
        ).fetchall()]
        evidence_links = []
        if "semantic_evidence_link" in ontology_meta_tables(connection):
            evidence_links = [dict(row) for row in connection.execute(
                "SELECT * FROM semantic_evidence_link WHERE target_type='fact' AND target_id=? ORDER BY created_at",
                (fact_id,),
            ).fetchall()]
        return {
            "schemaVersion": "explain-v1",
            "fact": dict(fact),
            "inputFacts": input_facts,
            "derivations": derivations,
            "decisions": decisions,
            "evidenceLinks": evidence_links,
            "traceId": ontology_trace_id("FACT", fact_id),
            "sourceWrite": False,
            "formalPublication": False,
        }
    finally:
        connection.close()


@domain_get("/api/world-model/decisions/{decision_id}/explain")
def world_model_decision_explain(decision_id: str) -> dict[str, Any]:
    connection = unified_semantics_connection()
    try:
        decision = connection.execute("SELECT * FROM semantic_rule_decision WHERE decision_id=?", (decision_id,)).fetchone()
        if decision is None:
            raise HTTPException(status_code=404, detail="规则判断不存在")
        input_ids = parse_json_array(decision["input_fact_ids_json"])
        input_facts: list[dict[str, Any]] = []
        if input_ids:
            marks = ",".join("?" for _ in input_ids)
            input_facts = [dict(row) for row in connection.execute(f"SELECT * FROM semantic_fact WHERE fact_id IN ({marks})", input_ids).fetchall()]
        derived_facts = [dict(row) for row in connection.execute(
            "SELECT f.* FROM semantic_fact f JOIN semantic_fact_derivation d ON d.output_fact_id=f.fact_id WHERE d.decision_id=? ORDER BY f.created_at",
            (decision_id,),
        ).fetchall()]
        actions = [dict(row) for row in connection.execute(
            "SELECT * FROM semantic_action_plan WHERE decision_id=? ORDER BY created_at",
            (decision_id,),
        ).fetchall()] if "semantic_action_plan" in ontology_meta_tables(connection) else []
        rule = connection.execute(
            "SELECT * FROM semantic_logic_rule WHERE rule_asset_id=? AND rule_version_id=?",
            (decision["rule_asset_id"], decision["rule_version_id"]),
        ).fetchone()
        if rule is None:
            rule = connection.execute(
                "SELECT asset_id AS rule_asset_id,current_version AS rule_version_id,title AS decision,canonical_definition AS explanation,status FROM knowledge_asset WHERE asset_id=? OR asset_key=? LIMIT 1",
                (decision["rule_asset_id"], decision["rule_asset_id"]),
            ).fetchone()
        return {
            "schemaVersion": "explain-v1",
            "decision": dict(decision),
            "rule": dict(rule) if rule else None,
            "inputFacts": input_facts,
            "derivedFacts": derived_facts,
            "actions": actions,
            "traceId": ontology_trace_id("DECISION", decision_id),
            "sourceWrite": False,
            "formalPublication": False,
        }
    finally:
        connection.close()


@domain_get("/api/rule-agent/profile")
def rule_agent_profile_endpoint(sample_size: int = Query(default=120, ge=20, le=500)) -> dict[str, Any]:
    sqlite = sqlite_connection()
    try:
        return {"profile": rule_agent_profile(sqlite, sample_size), "configured": bool(RULE_AGENT_API_KEY and RULE_AGENT_BASE_URL), "sourceWrite": False}
    finally:
        sqlite.close()


@domain_get("/api/rule-agent/status")
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


@domain_post("/api/semantic-reasoning/analyze")
def semantic_reasoning_analyze(request: SemanticReasoningRequest) -> dict[str, Any]:
    """Reason over context clusters without creating candidates or publications."""
    sqlite = sqlite_connection()
    run_id = f"semantic-reasoning-{uuid.uuid4().hex}"
    try:
        existing = sqlite.execute(
            "SELECT reasoning_run_id FROM semantic_reasoning_run WHERE idempotency_key=?",
            (request.idempotency_key,),
        ).fetchone()
        if existing is not None:
            run = sqlite.execute(
                "SELECT * FROM semantic_reasoning_run WHERE reasoning_run_id=?",
                (existing["reasoning_run_id"],),
            ).fetchone()
            items = sqlite.execute(
                "SELECT * FROM semantic_reasoning_item WHERE reasoning_run_id=? ORDER BY reasoning_item_id",
                (existing["reasoning_run_id"],),
            ).fetchall()
            return {
                "runId": run["reasoning_run_id"],
                "status": run["status"],
                "clusterCount": int(run["cluster_count"]),
                "sampleCount": int(run["sampled_count"]),
                "items": [semantic_reasoning_item_payload(row) for row in items],
                "sourceWrite": False,
                "formalPublication": False,
            }

        batch = latest_batch(sqlite)
        profile = rule_agent_profile(sqlite, request.sample_size)
        clusters = profile.get("semanticClusters", [])
        requested_keys = {str(value).strip() for value in request.cluster_keys if str(value).strip()}
        if requested_keys:
            clusters = [item for item in clusters if item.get("clusterKey") in requested_keys]
        clusters = clusters[: request.cluster_limit]
        now = utc_now()
        sqlite.execute(
            """
            INSERT INTO semantic_reasoning_run
              (reasoning_run_id,idempotency_key,batch_id,source_snapshot_id,cluster_count,sampled_count,
               model,provider_base_url,profile_json,status,created_at,source_write,formal_publication)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                run_id,
                request.idempotency_key,
                batch["batch_id"],
                batch["source_snapshot_id"],
                len(clusters),
                int(profile.get("sampleCount", 0)),
                RULE_AGENT_MODEL,
                RULE_AGENT_BASE_URL,
                json.dumps({"profile": profile, "selectedClusters": clusters}, ensure_ascii=False),
                "running",
                now,
                0,
                0,
            ),
        )
        sqlite.commit()
        raw_items = invoke_semantic_reasoning_agent(clusters) if clusters else []
        reasoning_items = canonicalize_semantic_reasoning(raw_items, clusters)
        sqlite.execute("BEGIN IMMEDIATE")
        for index, item in enumerate(reasoning_items, start=1):
            sqlite.execute(
                """
                INSERT INTO semantic_reasoning_item
                  (reasoning_item_id,reasoning_run_id,cluster_key,decision,hypothesis,evidence_json,
                   counterexamples_json,candidate_rule_json,confidence,risk_level,required_checks_json,created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    f"{run_id}-item-{index}",
                    run_id,
                    item["clusterKey"],
                    item["decision"],
                    item["hypothesis"],
                    json.dumps(item["evidence"], ensure_ascii=False),
                    json.dumps(item["counterexamples"], ensure_ascii=False),
                    json.dumps(item["candidateRule"], ensure_ascii=False),
                    item["confidence"],
                    item["riskLevel"],
                    json.dumps(item["requiredChecks"], ensure_ascii=False),
                    now,
                ),
            )
        sqlite.execute(
            "UPDATE semantic_reasoning_run SET status='completed',finished_at=? WHERE reasoning_run_id=?",
            (utc_now(), run_id),
        )
        sqlite.execute(
            """
            INSERT INTO audit_event(entity_type,entity_id,event_type,actor,payload_json,event_at)
            VALUES (?,?,?,?,?,?)
            """,
            (
                "semantic_reasoning",
                run_id,
                "semantic_reasoning_completed",
                "semantic-reasoning-agent",
                json.dumps(
                    {
                        "runId": run_id,
                        "clusterCount": len(clusters),
                        "sampleCount": int(profile.get("sampleCount", 0)),
                        "itemCount": len(reasoning_items),
                        "sourceWrite": False,
                        "formalPublication": False,
                        "note": request.note,
                    },
                    ensure_ascii=False,
                ),
                utc_now(),
            ),
        )
        sqlite.commit()
        return {
            "runId": run_id,
            "status": "completed",
            "clusterCount": len(clusters),
            "sampleCount": int(profile.get("sampleCount", 0)),
            "items": reasoning_items,
            "sourceWrite": False,
            "formalPublication": False,
        }
    except HTTPException as exc:
        if sqlite.in_transaction:
            sqlite.rollback()
        sqlite.execute(
            "UPDATE semantic_reasoning_run SET status='failed',error_message=?,finished_at=? WHERE reasoning_run_id=?",
            (str(exc.detail), utc_now(), run_id),
        )
        sqlite.commit()
        raise
    except Exception as exc:
        if sqlite.in_transaction:
            sqlite.rollback()
        sqlite.execute(
            "UPDATE semantic_reasoning_run SET status='failed',error_message=?,finished_at=? WHERE reasoning_run_id=?",
            (type(exc).__name__, utc_now(), run_id),
        )
        sqlite.commit()
        raise HTTPException(status_code=502, detail=f"语义推理执行失败：{type(exc).__name__}") from exc
    finally:
        sqlite.close()


@domain_get("/api/semantic-reasoning/latest")
def semantic_reasoning_latest() -> dict[str, Any]:
    sqlite = sqlite_connection()
    try:
        run = sqlite.execute("SELECT * FROM semantic_reasoning_run ORDER BY created_at DESC LIMIT 1").fetchone()
        if run is None:
            return {"run": None, "items": [], "sourceWrite": False, "formalPublication": False}
        items = sqlite.execute(
            "SELECT * FROM semantic_reasoning_item WHERE reasoning_run_id=? ORDER BY reasoning_item_id",
            (run["reasoning_run_id"],),
        ).fetchall()
        return {
            "run": {
                "runId": run["reasoning_run_id"],
                "status": run["status"],
                "clusterCount": int(run["cluster_count"]),
                "sampleCount": int(run["sampled_count"]),
                "model": run["model"],
                "createdAt": run["created_at"],
                "finishedAt": run["finished_at"],
            },
            "items": [semantic_reasoning_item_payload(row) for row in items],
            "sourceWrite": False,
            "formalPublication": False,
        }
    finally:
        sqlite.close()


@domain_get("/api/rule-agent/proposals")
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


@domain_post("/api/rule-agent/ai-review")
def ai_review_rule_agent_proposals(request: RuleAgentReviewRequest) -> dict[str, Any]:
    """Review rule proposals in one model call and store only recommendations."""
    sqlite = sqlite_connection()
    try:
        prior = sqlite.execute(
            "SELECT payload_json FROM audit_event WHERE event_type='rule_agent_ai_review_completed' ORDER BY event_id DESC LIMIT 50"
        ).fetchall()
        for event in prior:
            try:
                payload = json.loads(event["payload_json"])
            except json.JSONDecodeError:
                continue
            if payload.get("idempotencyKey") == request.idempotency_key:
                return payload

        if request.proposal_ids:
            marks = ",".join("?" for _ in request.proposal_ids)
            rows = sqlite.execute(
                f"SELECT * FROM rule_agent_proposal WHERE discovery_filter_status='eligible' AND proposal_id IN ({marks}) ORDER BY proposal_id",
                tuple(request.proposal_ids),
            ).fetchall()
        else:
            rows = sqlite.execute(
                "SELECT * FROM rule_agent_proposal WHERE discovery_filter_status='eligible' AND status IN ('draft','previewed','replayed') ORDER BY updated_at DESC LIMIT 20"
            ).fetchall()
        if not rows:
            return {
                "status": "empty",
                "reviewedCount": 0,
                "acceptedCount": 0,
                "needsReviewCount": 0,
                "rejectedCount": 0,
                "proposals": [],
                "sourceWrite": False,
                "formalPublication": False,
            }

        rows = [enrich_rule_agent_proposal_from_local_preview(sqlite, row) for row in rows]
        sqlite.commit()
        model_reviews = {str(item.get("proposalId")): item for item in invoke_rule_agent_review(rows)}
        now = utc_now()
        accepted = 0
        needs_review = 0
        rejected = 0
        sqlite.execute("BEGIN IMMEDIATE")
        for row in rows:
            decision, confidence, reason = deterministic_rule_review_gate(
                row,
                model_reviews.get(row["proposal_id"], {"decision": "needs_review", "reason": "智能体未返回该规则的审核结果"}),
            )
            if decision == "accept_rule":
                accepted += 1
            elif decision == "reject":
                rejected += 1
            else:
                needs_review += 1
            sqlite.execute(
                """
                UPDATE rule_agent_proposal
                SET agent_review_decision=?,agent_review_confidence=?,agent_review_reason=?,
                    agent_review_version=?,agent_reviewed_at=?,updated_at=?
                WHERE proposal_id=?
                """,
                (decision, confidence, reason, AGENT_REVIEW_VERSION, now, now, row["proposal_id"]),
            )
        auto_processed = 0
        for row in rows:
            refreshed = rule_agent_proposal_row(sqlite, row["proposal_id"])
            if refreshed["agent_review_decision"] == "accept_rule":
                refreshed = auto_process_accepted_rule_agent_proposal(sqlite, refreshed)
                if refreshed["status"] == "replayed":
                    auto_processed += 1
        payload = {
            "status": "completed",
            "reviewedCount": len(rows),
            "acceptedCount": accepted,
            "needsReviewCount": needs_review,
            "rejectedCount": rejected,
            "autoProcessedCount": auto_processed,
            "idempotencyKey": request.idempotency_key,
            "reviewVersion": AGENT_REVIEW_VERSION,
            "sourceWrite": False,
            "formalPublication": False,
        }
        sqlite.execute(
            "INSERT INTO audit_event(entity_type,entity_id,event_type,actor,payload_json,event_at) VALUES (?,?,?,?,?,?)",
            ("rule_agent", f"ai-review-{uuid.uuid4().hex}", "rule_agent_ai_review_completed", "rule-agent-review", json.dumps({**payload, "note": request.note}, ensure_ascii=False), now),
        )
        sqlite.commit()
        saved = sqlite.execute(
            "SELECT * FROM rule_agent_proposal WHERE proposal_id IN ({}) ORDER BY proposal_id".format(",".join("?" for _ in rows)),
            tuple(row["proposal_id"] for row in rows),
        ).fetchall()
        return {**payload, "proposals": [rule_agent_proposal_payload(row) for row in saved]}
    except HTTPException:
        if sqlite.in_transaction:
            sqlite.rollback()
        raise
    finally:
        sqlite.close()


@domain_post("/api/rule-agent/discover")
def discover_rules(request: RuleAgentRunRequest) -> dict[str, Any]:
    sqlite = sqlite_connection()
    run_id = f"rule-agent-{uuid.uuid4().hex}"
    try:
        existing = sqlite.execute("SELECT run_id FROM rule_agent_run WHERE idempotency_key=?", (request.idempotency_key,)).fetchone()
        if existing:
            rows = sqlite.execute("SELECT * FROM rule_agent_proposal WHERE run_id=? ORDER BY proposal_id", (existing["run_id"],)).fetchall()
            audit = sqlite.execute(
                "SELECT payload_json FROM audit_event WHERE entity_id=? AND event_type='rule_agent_discovery_completed' ORDER BY event_id DESC LIMIT 1",
                (existing["run_id"],),
            ).fetchone()
            audit_payload: dict[str, Any] = {}
            if audit:
                try:
                    parsed_audit = json.loads(audit["payload_json"] or "{}")
                    if isinstance(parsed_audit, dict):
                        audit_payload = parsed_audit
                except json.JSONDecodeError:
                    pass
            return {
                "runId": existing["run_id"],
                "status": "replayed",
                "proposals": [rule_agent_proposal_payload(row) for row in rows],
                "proposalCount": len(rows),
                "filterStats": audit_payload.get("filterStats"),
                "sourceWrite": False,
                "formalPublication": False,
            }
        batch = latest_batch(sqlite)
        profile = rule_agent_profile(sqlite, request.sample_size)
        now = utc_now()
        sqlite.execute(
            "INSERT INTO rule_agent_run(run_id,idempotency_key,batch_id,source_snapshot_id,eligible_count,sampled_count,model,provider_base_url,profile_json,status,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, request.idempotency_key, batch["batch_id"], batch["source_snapshot_id"], profile["eligibleCount"], profile["sampleCount"], RULE_AGENT_MODEL, RULE_AGENT_BASE_URL, json.dumps(profile, ensure_ascii=False), "running", now),
        )
        sqlite.commit()
        model_proposals = invoke_rule_agent(profile)
        proposals, dsl_rejected, fallback_used = canonicalize_rule_agent_proposals(
            model_proposals,
            profile.get("localRuleCatalog", []),
        )
        saved: list[dict[str, Any]] = []
        filtered: list[dict[str, Any]] = [
            {
                "ruleKey": item.get("patternKey") or "",
                "title": "AI proposal rejected before local execution",
                "matchedCount": 0,
                "sampleCount": 0,
                "reason": item.get("reason") or "unsupported_or_missing_pattern_key",
                "operation": item.get("operation"),
                "condition": item.get("condition"),
                "parameters": item.get("parameters"),
                "scope": item.get("scope"),
            }
            for item in dsl_rejected
        ]
        for index, item in enumerate(proposals):
            rule_key = re.sub(r"[^a-z0-9_.-]+", "_", str(item.get("ruleKey") or f"agent.discovered.rule_{index + 1}").lower())[:180]
            title = str(item.get("title") or rule_key)[:200]
            operation = str(item.get("operation") or "needs_review")[:40]
            risk = str(item.get("riskLevel") or "high").lower()
            if risk not in {"low", "medium", "high"}: risk = "high"
            try:
                confidence = max(0.0, min(1.0, float(item.get("confidence") or 0)))
            except (TypeError, ValueError):
                confidence = 0.0
            condition = item.get("condition") if isinstance(item.get("condition"), dict) else {}
            parameters = item.get("parameters") if isinstance(item.get("parameters"), dict) else {}
            scope = item.get("scope") if isinstance(item.get("scope"), dict) else {}
            measurement = measure_rule_agent_proposal(
                sqlite,
                run_id,
                rule_key,
                operation,
                condition,
                parameters,
                scope,
            )
            if not measurement["eligible"]:
                filtered.append(
                    {
                        "ruleKey": rule_key,
                        "title": title,
                        "matchedCount": measurement["matchedCount"],
                        "sampleCount": measurement["sampleCount"],
                        "reason": measurement["filterReason"],
                    }
                )
                continue
            proposal_id = f"proposal-{run_id}-{index + 1}"
            rule_version = f"{rule_key}-agent-{datetime.now(timezone.utc).strftime('%Y%m%d')}-v1"
            model_evidence = item.get("evidence") if isinstance(item.get("evidence"), dict) else {}
            try:
                model_expected_count = int(item.get("expectedCount") or 0)
            except (TypeError, ValueError):
                model_expected_count = 0
            evidence = {
                **model_evidence,
                "modelExpectedCount": model_expected_count,
                "matchedCount": measurement["matchedCount"],
                "sampleCount": measurement["sampleCount"],
                "measuredFromLocalPreview": True,
                "evidenceSource": "local_candidate_executor",
                "filterVersion": RULE_AGENT_DISCOVERY_FILTER_VERSION,
            }
            sqlite.execute(
                "INSERT OR IGNORE INTO rule_agent_proposal(proposal_id,run_id,rule_key,rule_version,title,objective,operation,condition_json,parameters_json,scope_json,evidence_json,examples_json,expected_count,confidence,risk_level,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (proposal_id, run_id, rule_key, rule_version, title, str(item.get("objective") or "")[:1000], operation, json.dumps(condition, ensure_ascii=False), json.dumps(parameters, ensure_ascii=False), json.dumps(scope, ensure_ascii=False), json.dumps(evidence, ensure_ascii=False), json.dumps(measurement["examples"], ensure_ascii=False), measurement["matchedCount"], confidence, risk, "draft", now, now),
            )
            saved.append({"proposalId": proposal_id, "ruleKey": rule_key})
        sqlite.execute(
            "UPDATE rule_agent_run SET status='completed',finished_at=?,attempt_count=CASE WHEN attempt_count=0 THEN 1 ELSE attempt_count END,fallback_used=? WHERE run_id=?",
            (utc_now(), int(bool(fallback_used)), run_id),
        )
        filter_stats = {
            "version": RULE_AGENT_DISCOVERY_FILTER_VERSION,
            "modelProposalCount": len(model_proposals),
            "eligibleProposalCount": int(sqlite.execute("SELECT count(*) FROM rule_agent_proposal WHERE run_id=?", (run_id,)).fetchone()[0]),
            "filteredProposalCount": len(filtered),
            "dslRejectedProposalCount": len(dsl_rejected),
            "fallbackUsed": fallback_used,
            "catalogVersion": RULE_AGENT_CATALOG_VERSION,
            "catalogBlockedCount": len(profile.get("blockedRuleCatalog", [])),
            "catalogBlocked": profile.get("blockedRuleCatalog", []),
            "minMatchedCount": RULE_AGENT_MIN_MATCH_COUNT,
            "minEvidenceSamples": RULE_AGENT_MIN_EVIDENCE_SAMPLES,
            "filtered": filtered,
        }
        sqlite.execute("INSERT INTO audit_event(entity_type,entity_id,event_type,actor,payload_json,event_at) VALUES (?,?,?,?,?,?)", ("rule_agent_run", run_id, "rule_agent_discovery_completed", "rule-agent", json.dumps({"runId": run_id, "eligibleCount": profile["eligibleCount"], "sampleCount": profile["sampleCount"], "proposalCount": filter_stats["eligibleProposalCount"], "modelProposalCount": len(model_proposals), "filteredProposalCount": len(filtered), "filterStats": filter_stats, "sourceWrite": False, "formalPublication": False}, ensure_ascii=False), utc_now()))
        sqlite.commit()
        rows = sqlite.execute("SELECT * FROM rule_agent_proposal WHERE run_id=? ORDER BY proposal_id", (run_id,)).fetchall()
        return {"runId": run_id, "status": "completed", "profile": profile, "proposals": [rule_agent_proposal_payload(row) for row in rows], "proposalCount": len(rows), "filterStats": filter_stats, "sourceWrite": False, "formalPublication": False}
    except Exception as exc:
        if sqlite.in_transaction:
            sqlite.rollback()
        if isinstance(exc, HTTPException):
            error_message = str(exc.detail)
            error_code, retryable, attempts = rule_agent_failure_fields(exc.detail)
        else:
            error_message = type(exc).__name__
            error_code, retryable, attempts = "unhandled_agent_error", False, 0
        sqlite.execute(
            "UPDATE rule_agent_run SET status='failed',error_message=?,error_code=?,retryable=?,attempt_count=?,finished_at=? WHERE run_id=?",
            (error_message, error_code, int(retryable), attempts, utc_now(), run_id),
        )
        sqlite.commit()
        if isinstance(exc, HTTPException):
            raise
        raise HTTPException(status_code=502, detail=f"规则智能体执行失败：{type(exc).__name__}") from exc
    finally:
        sqlite.close()


@domain_post("/api/rule-agent/proposals/{proposal_id}/preview")
def preview_rule_agent_proposal(proposal_id: str, request: RuleAgentProposalActionRequest) -> dict[str, Any]:
    sqlite = sqlite_connection()
    try:
        proposal = rule_agent_proposal_row(sqlite, proposal_id)
        if proposal["status"] in {"confirmed", "enabled"}:
            raise HTTPException(status_code=409, detail="规则草案已经确认或启用，不能回退重做预览")
        rows = rule_agent_target_rows(sqlite, proposal)
        if not rows:
            raise HTTPException(status_code=409, detail="规则草案在当前高质量范围内没有命中记录")
        preview_path, sample_path, preview_sha = rule_agent_write_preview(proposal, rows)
        now = utc_now()
        sqlite.execute(
            """
            UPDATE rule_agent_proposal
            SET status='previewed',preview_path=?,sample_path=?,preview_sha256=?,preview_count=?,
                replay_count=0,replay_pass_count=0,replay_fail_count=0,
                evaluation_replay_id=NULL,evaluation_count=0,evaluation_pass_count=0,
                evaluation_fail_count=0,replay_message=NULL,updated_at=?
            WHERE proposal_id=?
            """,
            (str(preview_path), str(sample_path), preview_sha, len(rows), now, proposal_id),
        )
        sqlite.execute(
            "INSERT INTO audit_event(entity_type,entity_id,event_type,actor,payload_json,event_at) VALUES (?,?,?,?,?,?)",
            ("rule_agent_proposal", proposal_id, "rule_agent_preview_generated", "local-user", json.dumps({"proposalId": proposal_id, "previewCount": len(rows), "sampleCount": min(200, len(rows)), "sourceWrite": False, "formalPublication": False, "note": request.note}, ensure_ascii=False), now),
        )
        sqlite.commit()
        return {"proposal": rule_agent_proposal_payload(rule_agent_proposal_row(sqlite, proposal_id)), "sourceWrite": False, "formalPublication": False}
    finally:
        sqlite.close()


@domain_post("/api/rule-agent/proposals/{proposal_id}/replay")
def replay_rule_agent_proposal(proposal_id: str, request: RuleAgentProposalActionRequest) -> dict[str, Any]:
    sqlite = sqlite_connection()
    try:
        proposal = rule_agent_proposal_row(sqlite, proposal_id)
        if proposal["status"] not in {"previewed", "replayed"}:
            raise HTTPException(status_code=409, detail="必须先生成预览，才能回放")
        preview_path = Path(proposal["preview_path"] or "")
        if not preview_path.exists() or sha256_file(preview_path) != proposal["preview_sha256"]:
            raise HTTPException(status_code=409, detail="预览文件不存在或校验值已变化，请重新生成预览")
        with preview_path.open("r", encoding="utf-8-sig", newline="") as handle:
            preview_rows = list(csv.DictReader(handle))
        current_rows = rule_agent_target_rows(sqlite, proposal)
        current_by_id = {row["CANDIDATE_ID"]: row for row in current_rows}
        failures: list[dict[str, str]] = []
        for item in preview_rows:
            current = current_by_id.get(item.get("CANDIDATE_ID", ""))
            if current is None:
                failures.append({"candidateId": item.get("CANDIDATE_ID", ""), "reason": "scope_changed_or_candidate_missing"})
                continue
            for field in ("ORIGINAL_DESCRIPTION", "PROPOSED_DESCRIPTION", "SITEID", "ASSETNUM"):
                if item.get(field, "") != current.get(field, ""):
                    failures.append({"candidateId": item.get("CANDIDATE_ID", ""), "reason": f"{field.lower()}_changed"})
                    break
        if len(preview_rows) != len(current_rows):
            failures.append({"candidateId": "(scope)", "reason": "preview_current_count_mismatch"})
        preview_failures = list(failures)
        preview_passed = len(preview_rows) - len({item["candidateId"] for item in preview_failures if item["candidateId"] != "(scope)"})
        preview_failed = len(preview_rows) - max(0, preview_passed)
        if any(item["candidateId"] == "(scope)" for item in preview_failures):
            preview_failed = max(preview_failed, 1)
        evaluation: dict[str, Any] = {
            "replayId": None,
            "status": "not_run",
            "evaluationCount": 0,
            "passCount": 0,
            "failCount": 0,
            "failures": [],
        }
        if not preview_failures:
            evaluation_replay_id = f"replay-agent-eval-{proposal_id}-{(proposal['preview_sha256'] or '')[:12]}"
            evaluation = replay_rule_agent_against_evaluation_cases(sqlite, proposal, evaluation_replay_id)
            failures.extend(
                {"candidateId": item["caseId"], "reason": item["reason"]}
                for item in evaluation["failures"]
            )
        status = "passed" if not failures and evaluation["status"] == "passed" else "failed"
        now = utc_now()
        next_status = "replayed" if status == "passed" else "failed"
        sqlite.execute(
            """
            UPDATE rule_agent_proposal
            SET status=?,replay_count=?,replay_pass_count=?,replay_fail_count=?,
                evaluation_replay_id=?,evaluation_count=?,evaluation_pass_count=?,
                evaluation_fail_count=?,replay_message=?,updated_at=?
            WHERE proposal_id=?
            """,
            (
                next_status,
                len(preview_rows),
                preview_passed,
                preview_failed,
                evaluation["replayId"],
                evaluation["evaluationCount"],
                evaluation["passCount"],
                evaluation["failCount"],
                None if status == "passed" else json.dumps(
                    {"previewFailures": preview_failures[:50], "evaluationFailures": evaluation["failures"][:50]},
                    ensure_ascii=False,
                ),
                now,
                proposal_id,
            ),
        )
        sqlite.execute(
            "INSERT INTO audit_event(entity_type,entity_id,event_type,actor,payload_json,event_at) VALUES (?,?,?,?,?,?)",
            ("rule_agent_proposal", proposal_id, "rule_agent_replay_executed", "local-user", json.dumps({"proposalId": proposal_id, "previewEvaluationCount": len(preview_rows), "previewPassCount": preview_passed, "previewFailCount": preview_failed, "evaluationReplayId": evaluation["replayId"], "evaluationCount": evaluation["evaluationCount"], "evaluationPassCount": evaluation["passCount"], "evaluationFailCount": evaluation["failCount"], "status": status, "sourceWrite": False, "formalPublication": False, "note": request.note}, ensure_ascii=False), now),
        )
        sqlite.commit()
        return {"proposal": rule_agent_proposal_payload(rule_agent_proposal_row(sqlite, proposal_id)), "status": status, "failures": failures[:200], "sourceWrite": False, "formalPublication": False}
    finally:
        sqlite.close()


@domain_post("/api/rule-agent/proposals/{proposal_id}/confirm")
def confirm_rule_agent_proposal(proposal_id: str, request: RuleAgentProposalActionRequest, actor: str = Depends(require_decision_auth)) -> dict[str, Any]:
    sqlite = sqlite_connection()
    try:
        proposal = rule_agent_proposal_row(sqlite, proposal_id)
        if proposal["status"] == "confirmed" or proposal["status"] == "enabled":
            return {"proposal": rule_agent_proposal_payload(proposal), "status": "confirmed", "sourceWrite": False, "formalPublication": False}
        if (
            proposal["status"] != "replayed"
            or proposal["replay_fail_count"] != 0
            or proposal["evaluation_count"] <= 0
            or proposal["evaluation_fail_count"] != 0
        ):
            raise HTTPException(status_code=409, detail="只有回放全部通过的规则草案才能人工确认")
        now = utc_now()
        sqlite.execute("UPDATE rule_agent_proposal SET status='confirmed',updated_at=? WHERE proposal_id=?", (now, proposal_id))
        sqlite.execute(
            "INSERT INTO audit_event(entity_type,entity_id,event_type,actor,payload_json,event_at) VALUES (?,?,?,?,?,?)",
            ("rule_agent_proposal", proposal_id, "rule_agent_proposal_confirmed", actor, json.dumps({"proposalId": proposal_id, "sourceWrite": False, "formalPublication": False, "note": request.note}, ensure_ascii=False), now),
        )
        sqlite.commit()
        return {"proposal": rule_agent_proposal_payload(rule_agent_proposal_row(sqlite, proposal_id)), "status": "confirmed", "sourceWrite": False, "formalPublication": False}
    finally:
        sqlite.close()


@domain_post("/api/rule-agent/proposals/{proposal_id}/enable")
def enable_rule_agent_proposal(proposal_id: str, request: RuleAgentProposalActionRequest, actor: str = Depends(require_decision_auth)) -> dict[str, Any]:
    sqlite = sqlite_connection()
    try:
        proposal = rule_agent_proposal_row(sqlite, proposal_id)
        if proposal["status"] == "enabled":
            return {"proposal": rule_agent_proposal_payload(proposal), "status": "enabled", "sourceWrite": False, "formalPublication": False}
        if proposal["status"] != "confirmed":
            raise HTTPException(status_code=409, detail="只有人工确认的规则草案才能启用")
        if proposal["evaluation_count"] <= 0 or proposal["evaluation_fail_count"] != 0 or not proposal["evaluation_replay_id"]:
            raise HTTPException(status_code=409, detail="规则启用前必须完成全量历史评价回放且失败为 0")
        replay_id = proposal["evaluation_replay_id"]
        now = utc_now()
        metadata = {"operation": proposal["operation"], "condition": json.loads(proposal["condition_json"] or "{}"), "parameters": json.loads(proposal["parameters_json"] or "{}"), "scope": json.loads(proposal["scope_json"] or "{}"), "proposalId": proposal_id, "evaluationReplayId": replay_id, "evaluationCount": int(proposal["evaluation_count"]), "evaluationFailCount": int(proposal["evaluation_fail_count"])}
        sqlite.execute(
            "INSERT INTO cleaning_rule_registry(rule_key,cleaning_type,rule_label,action_label,is_cleaning,replay_id,rule_version,enabled,metadata_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (proposal["rule_key"], "agent_" + proposal["operation"], proposal["title"], "规则启用", 1, replay_id, proposal["rule_version"], 1, json.dumps(metadata, ensure_ascii=False), now, now),
        )
        sqlite.execute(
            "INSERT INTO cleaning_run(cleaning_run_id,rule_key,replay_id,status,source_type,stage,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (f"cleaning-run-{replay_id}", proposal["rule_key"], replay_id, "approved", "rule_agent", "approved", now, now),
        )
        sqlite.execute("UPDATE rule_agent_proposal SET status='enabled',enabled_rule_key=?,updated_at=? WHERE proposal_id=?", (proposal["rule_key"], now, proposal_id))
        sqlite.execute(
            "INSERT INTO audit_event(entity_type,entity_id,event_type,actor,payload_json,event_at) VALUES (?,?,?,?,?,?)",
            ("rule_agent_proposal", proposal_id, "rule_agent_rule_enabled", actor, json.dumps({"proposalId": proposal_id, "ruleKey": proposal["rule_key"], "replayId": replay_id, "sourceWrite": False, "formalPublication": False, "note": request.note}, ensure_ascii=False), now),
        )
        sqlite.commit()
        return {"proposal": rule_agent_proposal_payload(rule_agent_proposal_row(sqlite, proposal_id)), "status": "enabled", "replayId": replay_id, "sourceWrite": False, "formalPublication": False}
    except sqlite3.IntegrityError as exc:
        if sqlite.in_transaction:
            sqlite.rollback()
        raise HTTPException(status_code=409, detail=f"规则启用冲突：{exc}") from exc
    finally:
        sqlite.close()


@domain_post("/api/rule-agent/proposals/{proposal_id}/queue")
def queue_rule_agent_proposal(proposal_id: str, request: RuleAgentProposalActionRequest, actor: str = Depends(require_decision_auth)) -> dict[str, Any]:
    """Send an enabled rule's current candidates into the common approval queue."""
    sqlite = sqlite_connection()
    try:
        prior = sqlite.execute(
            "SELECT payload_json FROM audit_event WHERE entity_type='rule_agent_proposal' AND entity_id=? AND event_type='rule_agent_candidates_queued' ORDER BY event_id DESC LIMIT 50",
            (proposal_id,),
        ).fetchall()
        for event in prior:
            try:
                payload = json.loads(event["payload_json"])
            except json.JSONDecodeError:
                continue
            if payload.get("idempotencyKey") == request.idempotency_key:
                return payload

        proposal = rule_agent_proposal_row(sqlite, proposal_id)
        if proposal["status"] != "enabled":
            raise HTTPException(status_code=409, detail="规则必须先完成回放、人工确认并启用，才能进入统一审批队列")
        replay = sqlite.execute(
            "SELECT status,fail_count FROM replay_run WHERE replay_id=?",
            (proposal["evaluation_replay_id"],),
        ).fetchone()
        if replay is None or replay["status"] != "passed" or int(replay["fail_count"] or 0) != 0:
            raise HTTPException(status_code=409, detail="规则历史评价回放未通过，不能进入审批队列")

        target_rows = rule_agent_target_rows(sqlite, proposal)
        if not target_rows:
            raise HTTPException(
                status_code=409,
                detail="当前规则关联的批次已没有待审批候选；请针对当前批次重新生成预览并回放后再入队",
            )
        now = utc_now()
        applied = 0
        skipped = 0
        existing_queue_ids = {
            row[0] for row in sqlite.execute("SELECT candidate_id FROM formal_approval_queue").fetchall()
        }
        sqlite.execute("BEGIN IMMEDIATE")
        for item in target_rows:
            candidate_id = item["CANDIDATE_ID"]
            if (
                candidate_id in existing_queue_ids
                or not str(item["PROPOSED_DESCRIPTION"] or "").strip()
            ):
                skipped += 1
                continue
            decision = "modified" if item["PROPOSED_DESCRIPTION"] != item["ORIGINAL_DESCRIPTION"] else "approved"
            sqlite.execute(
                """
                INSERT INTO formal_approval_queue
                  (queue_id,candidate_id,cluster_id,replay_id,proposed_decision,proposed_description,status,note,created_at,updated_at,source_write,formal_publication)
                VALUES (?,?,?,?,?,?,?,?,?,?,0,0)
                """,
                (
                    f"queue-rule-agent-{proposal_id}-{candidate_id}",
                    candidate_id,
                    proposal["rule_key"],
                    proposal["evaluation_replay_id"],
                    decision,
                    item["PROPOSED_DESCRIPTION"],
                    "pending",
                    f"规则智能体候选；规则版本 {proposal['rule_version']}；待人工审批",
                    now,
                    now,
                ),
            )
            applied += 1
        sync_cleaning_runs(sqlite, {proposal["evaluation_replay_id"]: dict(sqlite.execute("SELECT rule_key,cleaning_type,rule_label,action_label,is_cleaning,replay_id,rule_version,enabled FROM cleaning_rule_registry WHERE replay_id=?", (proposal["evaluation_replay_id"],)).fetchone())})
        payload = {
            "proposalId": proposal_id,
            "replayId": proposal["evaluation_replay_id"],
            "targetCount": len(target_rows),
            "appliedCount": applied,
            "skippedCount": skipped,
            "status": "queued",
            "idempotencyKey": request.idempotency_key,
            "sourceWrite": False,
            "formalPublication": False,
        }
        sqlite.execute(
            "INSERT INTO audit_event(entity_type,entity_id,event_type,actor,payload_json,event_at) VALUES (?,?,?,?,?,?)",
            ("rule_agent_proposal", proposal_id, "rule_agent_candidates_queued", actor, json.dumps(payload, ensure_ascii=False), now),
        )
        sqlite.commit()
        return payload
    except HTTPException:
        if sqlite.in_transaction:
            sqlite.rollback()
        raise
    except sqlite3.IntegrityError as exc:
        if sqlite.in_transaction:
            sqlite.rollback()
        raise HTTPException(status_code=409, detail=f"规则候选进入审批队列冲突：{exc}") from exc
    finally:
        sqlite.close()


@domain_get("/api/semantic/source-of-truth")
def semantic_source_of_truth() -> dict[str, Any]:
    """Report the canonical-read cutover coverage without changing data."""
    canonical = canonical_semantics_connection()
    identity = identity_result_connection()
    try:
        run = canonical_run_row(canonical)
        projected_devices = 0
        canonical_run_id = None
        if run is not None:
            canonical_run_id = run["run_id"]
            run_manifest = json.loads(run["manifest_json"] or "{}")
            scope = run_manifest.get("canonicalScope") if isinstance(run_manifest, dict) else None
            if isinstance(scope, dict) and scope.get("sourceIdentityDeviceCount") is not None:
                # Full identity projections record the exact count during the
                # streaming write; avoid rescanning tens of millions of local
                # statement/index rows on every UI refresh.
                projected_devices = int(scope["sourceIdentityDeviceCount"])
            else:
                projected_devices = int(canonical.execute(
                    """
                    SELECT count(DISTINCT subject_iri)
                    FROM canonical_statement
                    WHERE run_id=? AND predicate_iri=? AND object_iri=?
                    """,
                    (run["run_id"], "http://www.w3.org/1999/02/22-rdf-syntax-ns#type", "https://semantic.local/ontology/Device"),
                ).fetchone()[0])
        total_devices = int(identity.execute("SELECT count(*) FROM unified_device").fetchone()[0])
        coverage = (projected_devices / total_devices) if total_devices else 0.0
        return {
            "schemaVersion": "semantic-source-of-truth-v1",
            "status": "active" if total_devices and projected_devices == total_devices else "partial",
            "authority": "Canonical RDF Dataset",
            "canonicalRunId": canonical_run_id,
            "activeRelease": load_active_release(),
            "projectedDeviceCount": projected_devices,
            "identityDeviceCount": total_devices,
            "coverage": round(coverage, 6),
            "canonicalReadRoutes": [
                "/api/semantic/canonical/summary",
                "/api/semantic/canonical/statements",
                "/api/semantic/canonical/device/{source_namespace}/{canonical_key}",
                "/api/semantic/sparql",
                "/api/world-model/device/{unified_device_id}/context (projected objects)",
            ],
            "compatibilityReadRoutes": [
                "/api/world-model/device/{unified_device_id}/context (unprojected objects)",
                "/api/unified-devices*",
            ],
            "controlPlane": ["semantic_* mapping", "approval", "replay", "audit"],
            "sourceWrite": False,
            "formalPublication": False,
        }
    finally:
        identity.close()
        canonical.close()


def semantic_releases() -> dict[str, Any]:
    """Return the local Semantic Release registry and active pointer."""
    registry = load_semantic_release_registry()
    return {
        "schemaVersion": "semantic-release-v1",
        "active": load_active_release(),
        "releases": registry.get("releases", []),
        "sourceWrite": False,
        "formalPublication": False,
    }


def semantic_release_detail(release_id: str) -> dict[str, Any]:
    try:
        return load_semantic_release(release_id)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def semantic_release_backup(release_id: str, actor: str = Depends(require_decision_auth)) -> dict[str, Any]:
    """Create and checksum the immutable local RDF Dataset backup."""
    try:
        result = backup_release(release_id)
        result["actor"] = actor
        return result
    except (FileNotFoundError, FileExistsError, RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def semantic_release_approve(
    release_id: str,
    request: SemanticReleaseApprovalRequest,
    actor: str = Depends(require_decision_auth),
) -> dict[str, Any]:
    """Record production approval locally; it does not publish source data."""
    reviewer = request.reviewer.strip() or actor
    try:
        result = approve_release(release_id, reviewer, request.receipt, request.note)
        result["sourceWrite"] = False
        result["formalPublication"] = False
        return result
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def semantic_release_activate(release_id: str, actor: str = Depends(require_decision_auth)) -> dict[str, Any]:
    """Activate a previously approved and backed-up local Canonical release."""
    try:
        return activate_release(release_id, actor)
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def semantic_release_rollback(release_id: str, actor: str = Depends(require_decision_auth)) -> dict[str, Any]:
    """Move the local read pointer to a previously approved release."""
    try:
        return rollback_release(release_id, actor)
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def semantic_metrics() -> Response:
    """Prometheus-compatible local process metrics."""
    return Response(content=runtime_metrics.prometheus(), media_type="text/plain; version=0.0.4")


@domain_get("/api/semantic/canonical/summary")
def canonical_semantic_summary() -> dict[str, Any]:
    """Return the canonical RDF Dataset run, graphs and safety state."""
    connection = canonical_semantics_connection()
    try:
        run = canonical_run_row(connection)
        if run is None:
            return {"schemaVersion": "canonical-semantic-model-v1", "status": "not_initialized", "run": None, "graphs": [], "sourceWrite": False, "formalPublication": False}
        graphs = connection.execute(
            "SELECT graph_iri,graph_kind,source_snapshot_id,status FROM canonical_graph WHERE run_id=? ORDER BY graph_kind,graph_iri",
            (run["run_id"],),
        ).fetchall()
        manifest = json.loads(run["manifest_json"] or "{}")
        inference = None
        if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='canonical_inference_run'").fetchone():
            inference_row = connection.execute(
                "SELECT * FROM canonical_inference_run WHERE input_projection_run_id=? ORDER BY created_at DESC LIMIT 1",
                (run["run_id"],),
            ).fetchone()
            inference = dict(inference_row) if inference_row else None
        return {
            "schemaVersion": "canonical-semantic-model-v1",
            "status": run["status"],
            "run": dict(run),
            "standardBaseline": manifest.get("standardBaseline", []),
            "graphs": [dict(row) for row in graphs],
            "counts": {
                "resources": int(run["resource_count"]),
                "statements": int(run["statement_count"]),
                "provenance": int(connection.execute("SELECT count(*) FROM canonical_provenance WHERE statement_id IN (SELECT statement_id FROM canonical_statement WHERE run_id=?)", (run["run_id"],)).fetchone()[0]),
                "inferredStatements": int(inference["inferred_statement_count"]) if inference else 0,
            },
            "vocabulary": manifest.get("vocabulary", {}),
            "provenanceCoverage": manifest.get("provenanceCoverage", {}),
            "owlRlReplay": inference,
            "artifacts": manifest.get("artifacts", {}),
            "sourceWrite": False,
            "formalPublication": False,
        }
    finally:
        connection.close()


@domain_get("/api/semantic/canonical/statements")
def canonical_semantic_statements(
    subject_iri: str | None = Query(default=None, alias="subjectIri", max_length=1000),
    predicate_iri: str | None = Query(default=None, alias="predicateIri", max_length=1000),
    limit: int = Query(default=200, ge=1, le=1000),
) -> dict[str, Any]:
    """Read canonical RDF statements without exposing the source tables."""
    connection = canonical_semantics_connection()
    try:
        run = canonical_run_row(connection)
        if run is None:
            return {"schemaVersion": "canonical-semantic-model-v1", "runId": None, "rows": [], "sourceWrite": False, "formalPublication": False}
        where = ["run_id=?"]
        params: list[Any] = [run["run_id"]]
        if subject_iri:
            where.append("subject_iri=?")
            params.append(subject_iri)
        if predicate_iri:
            where.append("predicate_iri=?")
            params.append(predicate_iri)
        rows = connection.execute(
            f"SELECT graph_id,subject_iri,predicate_iri,object_kind,object_iri,lexical_value,datatype_iri,language_tag,confidence,assertion_status FROM canonical_statement WHERE {' AND '.join(where)} ORDER BY subject_iri,predicate_iri,statement_id LIMIT ?",
            [*params, limit],
        ).fetchall()
        inferred_rows: list[dict[str, Any]] = []
        if not subject_iri and not predicate_iri and connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='canonical_inference_run'").fetchone():
            inference = connection.execute("SELECT run_id,graph_iri FROM canonical_inference_run WHERE input_projection_run_id=? AND status='completed' ORDER BY created_at DESC LIMIT 1", (run["run_id"],)).fetchone()
            if inference:
                inferred_rows = [
                    {**dict(row), "graph_id": inference["graph_iri"], "assertion_status": "inferred", "confidence": None}
                    for row in connection.execute(
                        "SELECT subject_iri,predicate_iri,object_kind,object_iri,lexical_value,datatype_iri,language_tag FROM canonical_inferred_statement WHERE run_id=? ORDER BY subject_iri,predicate_iri,statement_id LIMIT ?",
                        (inference["run_id"], max(0, limit - len(rows))),
                    ).fetchall()
                ]
        return {"schemaVersion": "canonical-semantic-model-v1", "runId": run["run_id"], "rows": [dict(row) for row in rows] + inferred_rows, "sourceWrite": False, "formalPublication": False}
    finally:
        connection.close()


@domain_get("/api/semantic/canonical/device/{source_namespace}/{canonical_key}")
def canonical_semantic_device(source_namespace: str, canonical_key: str) -> dict[str, Any]:
    """Return one device as JSON-LD-compatible data plus provenance."""
    connection = canonical_semantics_connection()
    try:
        run = canonical_run_row(connection)
        if run is None:
            raise HTTPException(status_code=404, detail="Canonical Semantic Model 尚未完成投影")
        canonical_key_predicate = "https://semantic.local/ontology/canonicalKey"
        namespace_predicate = "https://semantic.local/ontology/sourceNamespace"
        subjects = connection.execute(
            """SELECT DISTINCT key_stmt.subject_iri
               FROM canonical_statement key_stmt
               JOIN canonical_statement namespace_stmt
                 ON namespace_stmt.run_id=key_stmt.run_id AND namespace_stmt.subject_iri=key_stmt.subject_iri
               WHERE key_stmt.run_id=? AND key_stmt.predicate_iri=? AND key_stmt.lexical_value=?
                 AND namespace_stmt.predicate_iri=? AND namespace_stmt.lexical_value=?""",
            (run["run_id"], canonical_key_predicate, canonical_key, namespace_predicate, source_namespace),
        ).fetchall()
        if not subjects:
            raise HTTPException(status_code=404, detail="Canonical Semantic Model 中不存在该设备")
        subject = subjects[0]["subject_iri"]
        rows = connection.execute(
            "SELECT statement_id,predicate_iri,object_kind,object_iri,lexical_value,datatype_iri,language_tag,confidence,assertion_status FROM canonical_statement WHERE run_id=? AND subject_iri=? ORDER BY predicate_iri,statement_id",
            (run["run_id"], subject),
        ).fetchall()
        document: dict[str, Any] = {"@id": subject, "@type": [], "@graphSource": "canonical-rdf-dataset"}
        provenance: list[dict[str, Any]] = []
        for row in rows:
            predicate = row["predicate_iri"]
            if predicate == "http://www.w3.org/1999/02/22-rdf-syntax-ns#type":
                document["@type"].append(row["object_iri"])
                continue
            if row["object_kind"] == "iri":
                value: Any = {"@id": row["object_iri"]}
            else:
                value = {"@value": row["lexical_value"]}
                if row["datatype_iri"]:
                    value["@type"] = row["datatype_iri"]
                if row["language_tag"]:
                    value["@language"] = row["language_tag"]
            document.setdefault(predicate, []).append(value)
            prov_rows = connection.execute(
                "SELECT source_system,source_schema,source_table,source_row_id,source_snapshot_id,activity_type,activity_id,evidence_json FROM canonical_provenance WHERE statement_id=? ORDER BY created_at",
                (row["statement_id"],),
            ).fetchall()
            provenance.extend(dict(item) for item in prov_rows)
        for key, value in list(document.items()):
            if isinstance(value, list) and len(value) == 1:
                document[key] = value[0]
        try:
            context_payload = json.loads(SEMANTIC_CONTEXT_FILE.read_text(encoding="utf-8"))
            jsonld_context = context_payload.get("@context", context_payload)
        except (OSError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=503, detail=f"Canonical JSON-LD context 不可用: {exc}") from exc
        return {
            "schemaVersion": "canonical-semantic-model-v1",
            "runId": run["run_id"],
            "jsonld": {"@context": jsonld_context, **document},
            "provenance": provenance,
            "sourceWrite": False,
            "formalPublication": False,
        }
    finally:
        connection.close()


@domain_post("/api/semantic/sparql")
def canonical_semantic_sparql(request: CanonicalSparqlRequest) -> dict[str, Any]:
    """Execute a bounded read-only SPARQL 1.1 SELECT/ASK query on the latest graph."""
    query = request.query.strip()

    guard = guard_text(query).upper()
    if not re.search(r"\b(SELECT|ASK)\b", guard) or re.search(r"\b(INSERT|DELETE|LOAD|CLEAR|DROP|CREATE|MOVE|COPY|ADD|SERVICE|UPDATE)\b", guard):
        raise HTTPException(status_code=422, detail="只允许只读 SPARQL SELECT/ASK；禁止更新、SERVICE 和外部访问")
    try:
        from rdflib import Dataset, Literal, URIRef
    except ImportError as exc:
        raise HTTPException(status_code=503, detail="后端缺少 rdflib，请安装 backend/requirements.txt") from exc
    connection = canonical_semantics_connection()
    try:
        run = canonical_run_row(connection)
        if run is None:
            raise HTTPException(status_code=404, detail="Canonical Semantic Model 尚未完成投影")
        dataset = Dataset()
        default_graph = dataset.default_context
        for row in connection.execute(
            "SELECT graph_id,subject_iri,predicate_iri,object_kind,object_iri,lexical_value,datatype_iri,language_tag FROM canonical_statement WHERE run_id=?",
            (run["run_id"],),
        ):
            graph_iri = connection.execute("SELECT graph_iri FROM canonical_graph WHERE graph_id=?", (row["graph_id"],)).fetchone()[0]
            graph = dataset.graph(URIRef(graph_iri))
            subject = URIRef(row["subject_iri"])
            predicate = URIRef(row["predicate_iri"])
            if row["object_kind"] == "iri":
                obj = URIRef(row["object_iri"])
            else:
                obj = Literal(row["lexical_value"] or "", datatype=URIRef(row["datatype_iri"]) if row["datatype_iri"] else None, lang=row["language_tag"])
            graph.add((subject, predicate, obj))
            # The endpoint exposes the union as the SPARQL default graph while
            # retaining named graphs for callers that need source/derived
            # provenance boundaries.
            default_graph.add((subject, predicate, obj))
        if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='canonical_inference_run'").fetchone():
            inference = connection.execute("SELECT run_id FROM canonical_inference_run WHERE input_projection_run_id=? AND status='completed' ORDER BY created_at DESC LIMIT 1", (run["run_id"],)).fetchone()
            if inference:
                for row in connection.execute(
                    "SELECT graph_iri,subject_iri,predicate_iri,object_kind,object_iri,lexical_value,datatype_iri,language_tag FROM canonical_inferred_statement WHERE run_id=?",
                    (inference["run_id"],),
                ):
                    graph = dataset.graph(URIRef(row["graph_iri"]))
                    subject = URIRef(row["subject_iri"])
                    predicate = URIRef(row["predicate_iri"])
                    obj = URIRef(row["object_iri"]) if row["object_kind"] == "iri" else Literal(row["lexical_value"] or "", datatype=URIRef(row["datatype_iri"]) if row["datatype_iri"] else None, lang=row["language_tag"])
                    graph.add((subject, predicate, obj))
                    default_graph.add((subject, predicate, obj))
        result = dataset.query(query)
        if result.type == "ASK":
            return {"schemaVersion": "sparql-1.1", "runId": run["run_id"], "type": "ASK", "boolean": bool(result.askAnswer), "sourceWrite": False, "formalPublication": False}
        bindings: list[dict[str, Any]] = []
        for row in result:
            if len(bindings) >= 500:
                break
            item: dict[str, Any] = {}
            for variable, value in row.asdict().items():
                if value is None:
                    continue
                if isinstance(value, URIRef):
                    item[str(variable)] = {"@id": str(value)}
                elif isinstance(value, Literal):
                    item[str(variable)] = {"@value": str(value), **({"@type": str(value.datatype)} if value.datatype else {}), **({"@language": value.language} if value.language else {})}
                else:
                    item[str(variable)] = str(value)
            bindings.append(item)
        return {"schemaVersion": "sparql-1.1", "runId": run["run_id"], "type": "SELECT", "vars": [str(value) for value in result.vars], "bindings": bindings, "truncated": len(bindings) >= 500, "sourceWrite": False, "formalPublication": False}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"SPARQL 查询无效：{exc}") from exc
    finally:
        connection.close()


@domain_get("/api/world-model/summary")
def world_model_summary() -> dict[str, Any]:
    """Return only materialized world-model capabilities from the local overlay."""
    connection = unified_semantics_connection()
    try:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }

        def count(table: str, where: str = "", parameters: tuple[Any, ...] = ()) -> int:
            if table not in tables:
                return 0
            suffix = f" WHERE {where}" if where else ""
            return int(connection.execute(f"SELECT count(*) FROM {table}{suffix}", parameters).fetchone()[0])

        identity = identity_result_connection()
        try:
            identity_tables = {
                row[0]
                for row in identity.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            }

            def identity_count(table: str) -> int:
                if table not in identity_tables:
                    return 0
                return int(identity.execute(f"SELECT count(*) FROM {table}").fetchone()[0])

            devices = identity_count("unified_device")
            locations = identity_count("function_location")
            identities = identity_count("device_identity_map")
        finally:
            identity.close()
        relations = count("business_object_relation", "status='accepted'")
        events = count(
            "semantic_fact",
            "fact_type IN ('observation_event','defect_event','work_order_event') AND status IN ('observed','accepted')",
        )
        facts = count("semantic_fact", "status<>'retracted'")
        canonical_state_facts = count("semantic_fact", "fact_type='canonical_defect_state' AND status IN ('derived','accepted')")
        current_state_count = count("semantic_current_state", "status='current'")
        transition_count = count("semantic_state_transition")
        rules = count("semantic_logic_rule", "status='enabled'")
        decisions = count("semantic_rule_decision", "status IN ('accepted','proposed','needs_review')")
        action_plans = count("semantic_action_plan")
        pending_action_approvals = count("semantic_action_plan", "status='PENDING_APPROVAL'")
        evidence_links = count("semantic_fact_derivation", "status IN ('accepted','proposed')")
        pending_status_mappings = count("semantic_status_dictionary", "mapping_status='pending'")
        approved_status_mappings = count("semantic_status_dictionary", "mapping_status='approved'")
        meta_model_run = None
        meta_model_count = 0
        meta_model_gap_count = 0
        if "ontology_meta_model_run" in tables:
            row = connection.execute("SELECT * FROM ontology_meta_model_run ORDER BY created_at DESC LIMIT 1").fetchone()
            meta_model_run = dict(row) if row else None
            if meta_model_run:
                meta_model_count = int(meta_model_run["object_type_count"]) + int(meta_model_run["property_type_count"]) + int(meta_model_run["relation_type_count"]) + int(meta_model_run["event_type_count"]) + int(meta_model_run["transition_rule_count"])
                meta_model_gap_count = int(meta_model_run["unregistered_relation_count"]) + int(meta_model_run["unregistered_event_count"])
        latest_replay = None
        if "semantic_status_replay_run" in tables:
            row = connection.execute(
                "SELECT * FROM semantic_status_replay_run ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
            latest_replay = dict(row) if row else None

        core = [
            {"key": "object", "label": "对象", "count": devices + locations, "status": "active" if devices + locations else "pending", "path": "/unified-devices", "description": f"设备 {devices:,} · 位置 {locations:,}"},
            {"key": "identity", "label": "身份", "count": identities, "status": "active" if identities else "pending", "path": "/unified-devices", "description": "源对象到统一业务对象的身份断言"},
            {"key": "relation", "label": "关系", "count": relations, "status": "active" if relations else "pending", "path": "/unified-devices", "description": "已确认的对象与业务记录关系"},
            {"key": "event", "label": "事件", "count": events, "status": "active" if events else "pending", "path": "/semantic-events", "description": "巡检、缺陷和工单来源事件"},
            {"key": "state", "label": "状态", "count": current_state_count, "status": "active" if current_state_count else ("blocked" if pending_status_mappings else "pending"), "path": "/semantic-status", "description": f"当前状态 {current_state_count} · 状态迁移 {transition_count}"},
            {"key": "fact", "label": "事实", "count": facts, "status": "active" if facts else "pending", "path": "/semantic-facts", "description": f"来源与派生事实；证据链 {evidence_links}"},
            {"key": "meta_model", "label": "元模型", "count": meta_model_count, "status": "active" if meta_model_run and meta_model_gap_count == 0 else ("in_progress" if meta_model_run else "pending"), "path": "/ontology-runtime", "description": f"对象/属性/关系/事件/迁移注册 {meta_model_count}；待复核缺口 {meta_model_gap_count}"},
            {"key": "rule", "label": "规则", "count": rules, "status": "active" if rules else "pending", "path": "/knowledge-assets", "description": "已启用的确定性规则"},
            {"key": "decision", "label": "决策", "count": decisions, "status": "active" if decisions else "pending", "path": "/decisions", "description": "规则判断，与事实分层保存"},
            {"key": "action", "label": "行动", "count": action_plans, "status": "governed" if action_plans else "pending", "path": "/decisions", "description": f"行动计划 {action_plans} · 待审批 {pending_action_approvals}；当前不写源系统"},
        ]
        stages = [
            {"key": "P0", "label": "标准缺陷状态", "status": "completed" if approved_status_mappings and latest_replay and latest_replay.get("status") == "completed" else "in_progress", "detail": f"已确认 {approved_status_mappings} · 待确认 {pending_status_mappings}"},
            {"key": "M1", "label": "本体元模型", "status": "completed" if meta_model_run and meta_model_gap_count == 0 else ("in_progress" if meta_model_run else "pending"), "detail": f"注册 {meta_model_count} 项 · 待复核缺口 {meta_model_gap_count}"},
            {"key": "P1", "label": "统一事件投影", "status": "completed" if events else "pending", "detail": f"已投影 {events} 条来源事件"},
            {"key": "P2", "label": "状态迁移引擎", "status": "completed" if current_state_count and latest_replay and latest_replay.get("status") == "completed" else "pending", "detail": f"当前状态 {current_state_count} · 迁移记录 {transition_count}"},
            {"key": "P3", "label": "分层事实", "status": "in_progress" if facts else "pending", "detail": f"当前事实 {facts} 条"},
            {"key": "P4", "label": "规则体系", "status": "in_progress" if rules else "pending", "detail": f"已启用确定性规则 {rules} 条"},
            {"key": "P5", "label": "决策与行动审批", "status": "completed" if action_plans and pending_action_approvals == 0 else ("in_progress" if decisions else "pending"), "detail": f"决策 {decisions} · 行动计划 {action_plans} · 待审批 {pending_action_approvals}"},
        ]
        return {
            "core": core,
            "stages": stages,
            "sourceWrite": False,
            "formalPublication": False,
            "sourceSystems": ["HD_SAAS", "XNY_SAAS", "MaxiEAM / DM8"],
            "latestStatusReplay": latest_replay,
            "latestOntologyMetaModel": meta_model_run,
            "latestStateTransition": dict(connection.execute("SELECT * FROM semantic_state_transition_run ORDER BY created_at DESC LIMIT 1").fetchone()) if "semantic_state_transition_run" in tables and connection.execute("SELECT 1 FROM semantic_state_transition_run LIMIT 1").fetchone() else None,
            "nextAction": {
                "title": "查看规则决策与行动门禁" if decisions else ("建设事件到状态的后续规则" if current_state_count else "确认缺陷状态映射并运行回放"),
                "description": f"已形成 {decisions} 条规则判断；行动计划 {action_plans} 条，待审批 {pending_action_approvals} 条。" if decisions else (f"已形成 {current_state_count} 条当前状态；下一步补充有时间顺序的事件状态迁移。" if current_state_count else f"{pending_status_mappings} 条原始缺陷状态尚未映射到标准状态。"),
                "path": "/decisions" if decisions else ("/semantic-facts" if current_state_count else "/semantic-status?mapping_status=pending"),
            },
        }
    finally:
        connection.close()


@domain_get("/api/world-model/coverage")
def world_model_coverage() -> dict[str, Any]:
    """Return the latest local evidence-coverage and closure report.

    Missing source evidence is returned as an explicit gap rather than being
    represented as zero business facts without explanation.  This endpoint
    never opens a source-system connection and never changes a review gate.
    """
    connection = unified_semantics_connection()
    try:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if "semantic_coverage_run" not in tables:
            return {
                "schemaVersion": "semantic-coverage-v1",
                "status": "not_initialized",
                "run": None,
                "gaps": [],
                "sourceWrite": False,
                "formalPublication": False,
            }
        run = connection.execute("SELECT * FROM semantic_coverage_run ORDER BY created_at DESC LIMIT 1").fetchone()
        gaps = connection.execute(
            """SELECT gap_id,gap_type,scope_key,expected_count,observed_count,status,
                      evidence_required,note,created_at
               FROM semantic_coverage_gap
               WHERE run_id=?
               ORDER BY CASE status WHEN 'needs_evidence' THEN 0 WHEN 'isolated' THEN 1 ELSE 2 END,
                        gap_type,scope_key""",
            (run["run_id"],),
        ).fetchall() if run else []
        return {
            "schemaVersion": "semantic-coverage-v1",
            "status": run["status"] if run else "not_initialized",
            "run": dict(run) if run else None,
            "gaps": [dict(gap) for gap in gaps],
            "sourceWrite": False,
            "formalPublication": False,
        }
    finally:
        connection.close()


@domain_get("/api/world-model/runtime-contract")
def world_model_runtime_contract() -> dict[str, Any]:
    """Return the source-authority, object and identity runtime contract."""
    connection = unified_semantics_connection()
    try:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if "semantic_runtime_contract_run" not in tables:
            return {"schemaVersion": "semantic-runtime-contract-v1", "status": "not_initialized", "run": None, "authority": [], "counts": {}, "sourceWrite": False, "formalPublication": False}
        run = connection.execute("SELECT * FROM semantic_runtime_contract_run ORDER BY created_at DESC LIMIT 1").fetchone()
        authority = connection.execute(
            "SELECT layer_key,layer_kind,display_name,precedence,source_of_record,requires_approval,applies_to_json,status,version FROM semantic_layer_authority WHERE status='active' ORDER BY precedence DESC,layer_key"
        ).fetchall()
        violation_rows = connection.execute(
            "SELECT constraint_key,status,severity,count(*) AS count FROM semantic_constraint_violation GROUP BY constraint_key,status,severity ORDER BY severity DESC,constraint_key"
        ).fetchall()
        counts = {
            "objectInstances": int(connection.execute("SELECT count(*) FROM semantic_object_instance").fetchone()[0]),
            "identityAssertions": int(connection.execute("SELECT count(*) FROM semantic_identity_assertion").fetchone()[0]),
            "events": int(connection.execute("SELECT count(*) FROM semantic_event WHERE status IN ('observed','accepted')").fetchone()[0]),
            "tracedEvents": int(connection.execute("SELECT count(*) FROM semantic_event WHERE provenance_hash IS NOT NULL AND previous_event_id IS NOT NULL OR provenance_hash IS NOT NULL").fetchone()[0]),
            "violations": int(connection.execute("SELECT count(*) FROM semantic_constraint_violation").fetchone()[0]),
        }
        return {
            "schemaVersion": "semantic-runtime-contract-v1",
            "status": run["status"] if run else "not_initialized",
            "run": dict(run) if run else None,
            "authority": [dict(row) for row in authority],
            "counts": counts,
            "violations": [dict(row) for row in violation_rows],
            "sourceWrite": False,
            "formalPublication": False,
        }
    finally:
        connection.close()


@domain_get("/api/world-model/governance-contract")
def world_model_governance_contract() -> dict[str, Any]:
    """Return typed identity, relation, schema, event and rule governance.

    This is a read-only view over the local semantic overlay.  It exposes
    isolated evidence gaps explicitly and never turns a missing relation into
    an inferred business fact.
    """
    connection = unified_semantics_connection()
    try:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        required = {"semantic_governance_quality_run", "semantic_identity_policy", "semantic_relation_contract", "semantic_object_property", "semantic_executable_rule"}
        if not required.issubset(tables):
            return {
                "schemaVersion": "semantic-governance-contract-v1",
                "status": "not_initialized",
                "run": None,
                "identity": {},
                "relations": [],
                "objectSchema": [],
                "eventRelations": [],
                "rules": [],
                "conflicts": [],
                "violations": [],
                "sourceWrite": False,
                "formalPublication": False,
            }
        run = connection.execute("SELECT * FROM semantic_governance_quality_run ORDER BY created_at DESC LIMIT 1").fetchone()
        policy = connection.execute("SELECT * FROM semantic_identity_policy WHERE status='active' ORDER BY effective_from DESC LIMIT 1").fetchone()
        identity_summary = {
            "policy": dict(policy) if policy else None,
            "assertionCount": int(connection.execute("SELECT count(*) FROM semantic_identity_assertion").fetchone()[0]),
            "autoCount": int(connection.execute("SELECT count(*) FROM semantic_identity_assertion WHERE decision_mode='auto'").fetchone()[0]),
            "manualCount": int(connection.execute("SELECT count(*) FROM semantic_identity_assertion WHERE decision_mode='manual'").fetchone()[0]),
            "reviewRequiredCount": int(connection.execute("SELECT count(*) FROM semantic_identity_assertion WHERE review_required=1").fetchone()[0]),
            "revokedCount": int(connection.execute("SELECT count(*) FROM semantic_identity_assertion WHERE lifecycle_status='revoked'").fetchone()[0]),
            "conflictCount": int(connection.execute("SELECT count(*) FROM semantic_identity_conflict WHERE status='isolated'").fetchone()[0]),
        }
        if "semantic_identity_review" in tables:
            identity_summary["reviewQueue"] = {
                str(row["queue_status"]): int(row["count"])
                for row in connection.execute("SELECT queue_status,count(*) AS count FROM semantic_identity_review GROUP BY queue_status").fetchall()
            }
        relation_contracts = [dict(row) for row in connection.execute(
            "SELECT relation_type,predicate,subject_type,object_type,min_cardinality,max_cardinality,temporal,evidence_required,version,review_status,status FROM semantic_relation_contract WHERE status='active' ORDER BY relation_type"
        ).fetchall()]
        object_schema = [dict(row) for row in connection.execute(
            "SELECT object_type,property_key,value_type,required,repeatable,unit,source_mapping_json,validation_json,version,review_status,status FROM semantic_object_property WHERE status='active' ORDER BY object_type,property_key"
        ).fetchall()]
        event_relations = [dict(row) for row in connection.execute(
            "SELECT relation_type,predicate,subject_event_type,object_event_type,min_cardinality,max_cardinality,evidence_required,version,review_status,status FROM semantic_event_relation_contract WHERE status='active' ORDER BY relation_type"
        ).fetchall()] if "semantic_event_relation_contract" in tables else []
        rules = [dict(row) for row in connection.execute(
            "SELECT rule_id,rule_version,rule_type,title,target_object_type,condition_json,result_schema_json,action_json,effective_from,effective_to,status,requires_approval,source_write,formal_publication FROM semantic_executable_rule WHERE status IN ('replayed','approved','enabled') ORDER BY rule_type,rule_id,rule_version"
        ).fetchall()]
        conflicts = [dict(row) for row in connection.execute(
            "SELECT conflict_id,conflict_type,source_schema,source_table,source_row_id,source_key_type,source_key,canonical_object_ids_json,assertion_ids_json,severity,status,evidence_json,detected_at FROM semantic_identity_conflict WHERE status='isolated' ORDER BY detected_at DESC,conflict_id"
        ).fetchall()]
        violations = [dict(row) for row in connection.execute(
            "SELECT constraint_key,entity_type,entity_id,severity,status,expected_json,actual_json,evidence_json,created_at FROM semantic_constraint_violation WHERE constraint_key LIKE 'governance_%' ORDER BY severity DESC,constraint_key,entity_id"
        ).fetchall()]
        return {
            "schemaVersion": "semantic-governance-contract-v1",
            "status": run["status"] if run else "not_initialized",
            "run": dict(run) if run else None,
            "identity": identity_summary,
            "relations": relation_contracts,
            "objectSchema": object_schema,
            "eventRelations": event_relations,
            "rules": rules,
            "conflicts": conflicts,
            "violations": violations,
            "sourceWrite": False,
            "formalPublication": False,
        }
    finally:
        connection.close()


@domain_post("/api/world-model/identity/{assertion_id}/revoke")
def revoke_semantic_identity(assertion_id: str, request: SemanticIdentityRevokeRequest, actor: str = Depends(require_decision_auth)) -> dict[str, Any]:
    """Revoke one local identity assertion with an auditable reason.

    Revocation changes only the local overlay lifecycle.  It deliberately does
    not change the source record, source snapshot, or canonical source key.
    """
    connection = unified_semantics_write_connection()
    try:
        assertion = connection.execute(
            "SELECT assertion_id,canonical_object_id,lifecycle_status,status,decision_mode FROM semantic_identity_assertion WHERE assertion_id=?",
            (assertion_id,),
        ).fetchone()
        if assertion is None:
            raise HTTPException(status_code=404, detail="身份断言不存在")
        existing = connection.execute(
            "SELECT audit_id,created_at,revoked_by,revocation_reason FROM semantic_identity_revocation_audit WHERE idempotency_key=?",
            (request.idempotency_key,),
        ).fetchone()
        if existing is not None:
            return {
                "status": "already_revoked",
                "assertion": dict(assertion),
                "audit": dict(existing),
                "sourceWrite": False,
                "formalPublication": False,
            }
        if assertion["lifecycle_status"] == "revoked":
            return {
                "status": "already_revoked",
                "assertion": dict(assertion),
                "audit": None,
                "sourceWrite": False,
                "formalPublication": False,
            }
        timestamp = utc_now()
        audit_id = f"SIRA-{hashlib.sha256((assertion_id + request.idempotency_key).encode('utf-8')).hexdigest()[:24]}"
        connection.execute(
            """
            INSERT INTO semantic_identity_revocation_audit(
              audit_id,assertion_id,previous_lifecycle_status,revoked_by,revocation_reason,idempotency_key,created_at
            ) VALUES (?,?,?,?,?,?,?)
            """,
            (audit_id, assertion_id, assertion["lifecycle_status"], request.reviewer.strip() or actor, request.reason.strip(), request.idempotency_key, timestamp),
        )
        connection.execute(
            """
            UPDATE semantic_identity_assertion
               SET lifecycle_status='revoked',revoked_at=?,revoked_by=?,revocation_reason=?,review_required=1,updated_at=?
             WHERE assertion_id=?
            """,
            (timestamp, request.reviewer.strip() or actor, request.reason.strip(), timestamp, assertion_id),
        )
        connection.commit()
        updated = connection.execute("SELECT * FROM semantic_identity_assertion WHERE assertion_id=?", (assertion_id,)).fetchone()
        return {
            "status": "revoked",
            "assertion": dict(updated) if updated else None,
            "audit": {"auditId": audit_id, "reviewer": request.reviewer.strip() or actor, "reason": request.reason.strip(), "createdAt": timestamp},
            "sourceWrite": False,
            "formalPublication": False,
        }
    except HTTPException:
        connection.rollback()
        raise
    except sqlite3.IntegrityError as exc:
        connection.rollback()
        raise HTTPException(status_code=409, detail=f"身份撤销审计冲突：{exc}") from exc
    finally:
        connection.close()


@domain_get("/api/world-model/identity-review")
def semantic_identity_review_queue(
    status: Literal["all", "pending", "approved", "rejected", "blocked"] = "pending",
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    """List local identity assertions that require an explicit decision."""
    connection = unified_semantics_connection()
    try:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if "semantic_identity_review" not in tables:
            return {"schemaVersion": "semantic-identity-review-v1", "status": "not_initialized", "rows": [], "total": 0, "page": page, "pageSize": page_size, "sourceWrite": False, "formalPublication": False}
        where = ""
        parameters: list[Any] = []
        if status != "all":
            where = "WHERE r.queue_status=?"
            parameters.append(status)
        total = int(connection.execute(f"SELECT count(*) FROM semantic_identity_review r {where}", parameters).fetchone()[0])
        parameters.extend([page_size, (page - 1) * page_size])
        rows = [dict(row) for row in connection.execute(
            f"""
            SELECT r.review_id,r.assertion_id,r.queue_status,r.reviewer,r.review_note,r.evidence_json,
                   r.approval_receipt,r.created_at,r.reviewed_at,r.updated_at,
                   a.source_system,a.source_schema,a.source_table_group,a.source_table,a.source_row_id,
                   a.source_key_type,a.source_key,a.canonical_object_type,a.canonical_object_id,
                   a.assertion_type,a.status AS assertion_status,a.confidence,a.valid_from,a.valid_to,
                   a.decision_mode,a.review_required,a.lifecycle_status,a.policy_version,a.evidence_json AS assertion_evidence_json,
                   a.source_snapshot_id
              FROM semantic_identity_review r
              JOIN semantic_identity_assertion a ON a.assertion_id=r.assertion_id
              {where}
             ORDER BY CASE r.queue_status WHEN 'pending' THEN 0 WHEN 'blocked' THEN 1 ELSE 2 END,
                      r.created_at,r.review_id
             LIMIT ? OFFSET ?
            """,
            parameters,
        ).fetchall()]
        conflict_by_assertion: dict[str, list[dict[str, Any]]] = {}
        if "semantic_identity_conflict" in tables:
            for conflict in connection.execute("SELECT * FROM semantic_identity_conflict WHERE status='isolated'").fetchall():
                item = dict(conflict)
                for assertion_id in parse_json_array(conflict["assertion_ids_json"]):
                    conflict_by_assertion.setdefault(assertion_id, []).append(item)
        for row in rows:
            row["conflicts"] = conflict_by_assertion.get(str(row["assertion_id"]), [])
            row["hasConflict"] = bool(row["conflicts"])
        counts = {
            str(row["queue_status"]): int(row["count"])
            for row in connection.execute("SELECT queue_status,count(*) AS count FROM semantic_identity_review GROUP BY queue_status ORDER BY queue_status").fetchall()
        }
        return {
            "schemaVersion": "semantic-identity-review-v1",
            "status": "ready",
            "rows": rows,
            "total": total,
            "page": page,
            "pageSize": page_size,
            "counts": counts,
            "sourceWrite": False,
            "formalPublication": False,
        }
    finally:
        connection.close()


@domain_get("/api/world-model/identity-review/{review_id}")
def semantic_identity_review_detail(review_id: str) -> dict[str, Any]:
    connection = unified_semantics_connection()
    try:
        row = connection.execute(
            """
            SELECT r.*,a.source_system,a.source_schema,a.source_table_group,a.source_table,a.source_row_id,
                   a.source_key_type,a.source_key,a.canonical_object_type,a.canonical_object_id,a.assertion_type,
                   a.status AS assertion_status,a.confidence,a.valid_from,a.valid_to,a.decision_mode,a.review_required,
                   a.lifecycle_status,a.policy_version,a.evidence_json AS assertion_evidence_json,a.source_snapshot_id
              FROM semantic_identity_review r
              JOIN semantic_identity_assertion a ON a.assertion_id=r.assertion_id
             WHERE r.review_id=?
            """,
            (review_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="身份审核记录不存在")
        conflicts = [dict(item) for item in connection.execute(
            "SELECT * FROM semantic_identity_conflict WHERE status='isolated' AND assertion_ids_json LIKE ? ORDER BY detected_at DESC",
            (f"%{row['assertion_id']}%",),
        ).fetchall()]
        audits = [dict(item) for item in connection.execute(
            "SELECT * FROM semantic_identity_review_audit WHERE review_id=? ORDER BY created_at DESC",
            (review_id,),
        ).fetchall()] if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='semantic_identity_review_audit'").fetchone() else []
        return {
            "schemaVersion": "semantic-identity-review-v1",
            "review": dict(row),
            "conflicts": conflicts,
            "audits": audits,
            "sourceWrite": False,
            "formalPublication": False,
        }
    finally:
        connection.close()


@domain_post("/api/world-model/identity-review/{review_id}")
def decide_semantic_identity_review(review_id: str, request: SemanticIdentityReviewRequest, actor: str = Depends(require_decision_auth)) -> dict[str, Any]:
    """Record a local identity review decision; source systems remain read-only."""
    connection = unified_semantics_write_connection()
    try:
        review = connection.execute("SELECT * FROM semantic_identity_review WHERE review_id=?", (review_id,)).fetchone()
        if review is None:
            raise HTTPException(status_code=404, detail="身份审核记录不存在")
        assertion = connection.execute("SELECT * FROM semantic_identity_assertion WHERE assertion_id=?", (review["assertion_id"],)).fetchone()
        if assertion is None:
            raise HTTPException(status_code=404, detail="身份断言不存在")
        prior = connection.execute("SELECT * FROM semantic_identity_review_audit WHERE idempotency_key=?", (request.idempotency_key,)).fetchone()
        if prior is not None:
            return {"status": "already_decided", "review": dict(review), "audit": dict(prior), "sourceWrite": False, "formalPublication": False}
        if review["queue_status"] != "pending":
            return {"status": "already_decided", "review": dict(review), "audit": None, "sourceWrite": False, "formalPublication": False}
        conflicts = connection.execute(
            "SELECT conflict_id,conflict_type,source_key_type,source_key FROM semantic_identity_conflict WHERE status='isolated' AND assertion_ids_json LIKE ?",
            (f"%{review['assertion_id']}%",),
        ).fetchall()
        if request.decision == "approved" and conflicts:
            raise HTTPException(status_code=409, detail="该身份断言属于冲突组，必须先处理同组其他映射；不能单条自动解除冲突")
        timestamp = utc_now()
        receipt = f"IRV-{hashlib.sha256((review_id + request.decision + request.idempotency_key).encode('utf-8')).hexdigest()[:24]}"
        audit_id = f"SIRA-{hashlib.sha256((review_id + request.idempotency_key).encode('utf-8')).hexdigest()[:24]}"
        connection.execute(
            """
            INSERT INTO semantic_identity_review_audit(
              audit_id,review_id,assertion_id,decision,reviewer,note,evidence_json,approval_receipt,idempotency_key,created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            (audit_id, review_id, review["assertion_id"], request.decision, request.reviewer.strip() or actor, request.note.strip(), json.dumps(request.evidence, ensure_ascii=False, sort_keys=True), receipt, request.idempotency_key, timestamp),
        )
        new_assertion_status = "accepted" if request.decision == "approved" else "rejected"
        new_link_status = "accepted" if request.decision == "approved" else "blocked"
        new_relation_status = "accepted" if request.decision == "approved" else "isolated"
        connection.execute(
            """
            UPDATE semantic_identity_review
               SET queue_status=?,reviewer=?,review_note=?,evidence_json=?,approval_receipt=?,idempotency_key=?,reviewed_at=?,updated_at=?
             WHERE review_id=?
            """,
            (request.decision, request.reviewer.strip() or actor, request.note.strip(), json.dumps(request.evidence, ensure_ascii=False, sort_keys=True), receipt, request.idempotency_key, timestamp, timestamp, review_id),
        )
        connection.execute(
            """
            UPDATE semantic_identity_assertion
               SET status=?,decision_mode='manual',review_required=0,updated_at=?
             WHERE assertion_id=?
            """,
            (new_assertion_status, timestamp, review["assertion_id"]),
        )
        connection.execute(
            """
            UPDATE business_record_link
               SET status=?
             WHERE source_schema=? AND source_table=? AND source_row_id=?
            """,
            (new_link_status, assertion["source_schema"], assertion["source_table"], assertion["source_row_id"]),
        )
        if "semantic_relation_assertion" in {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}:
            connection.execute(
                """
                UPDATE semantic_relation_assertion
                   SET status=?,updated_at=?
                 WHERE source_schema=? AND source_table=? AND source_row_id=?
                """,
                (new_relation_status, timestamp, assertion["source_schema"], assertion["source_table"], assertion["source_row_id"]),
            )
        connection.commit()
        updated = connection.execute("SELECT * FROM semantic_identity_review WHERE review_id=?", (review_id,)).fetchone()
        return {
            "status": request.decision,
            "review": dict(updated) if updated else None,
            "approvalReceipt": receipt,
            "replayRequired": True,
            "sourceWrite": False,
            "formalPublication": False,
        }
    except HTTPException:
        connection.rollback()
        raise
    except sqlite3.IntegrityError as exc:
        connection.rollback()
        raise HTTPException(status_code=409, detail=f"身份审核写入冲突：{exc}") from exc
    finally:
        connection.close()


@domain_get("/api/dashboard")
def dashboard() -> dict[str, Any]:
    sqlite = sqlite_connection()
    try:
        batch = latest_batch(sqlite)
        sample = ensure_review_sample(sqlite, batch)
        sample_info = review_sample_summary(sqlite, sample)
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
        "samplesReady": int(sample_info["pendingCount"]),
        "reviewSample": sample_info,
        "sites": [{"siteId": row[0], "count": int(row[1]), "share": float(row[2])} for row in sites],
        "contextCoverage": [
            {"label": "KKS / 位置", "value": percentage("location_code_rows")},
            {"label": "位置父级", "value": percentage("location_parent_rows")},
            {"label": "分类", "value": percentage("classification_rows")},
            {"label": "规格 / 特征", "value": percentage("specification_rows")},
        ],
    }


@domain_get("/api/review-sample")
def review_sample(sample_size: int = Query(default=DEFAULT_SAMPLE_TARGET, ge=1, le=MAX_SAMPLE_TARGET)) -> dict[str, Any]:
    sqlite = sqlite_connection()
    try:
        batch = latest_batch(sqlite)
        sample = ensure_review_sample(sqlite, batch, sample_size)
        return review_sample_summary(sqlite, sample)
    finally:
        sqlite.close()


@domain_get("/api/ai-review/sample")
def ai_review_sample(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    ai_decision: Literal["all", "keep_original", "accept_candidate", "needs_review"] = "all",
    site_id: str | None = None,
) -> dict[str, Any]:
    sample_id, sample_rows = load_ai_review_sample()
    sqlite = sqlite_connection()
    try:
        summary = ai_sample_summary(sqlite, sample_id, sample_rows)
        reviewed = {
            row["candidate_id"]: row
            for row in sqlite.execute(
                "SELECT candidate_id, decision, note, reviewer, reviewed_at FROM ai_review_decision WHERE sample_id=?",
                (sample_id,),
            ).fetchall()
        }
        filtered: list[dict[str, Any]] = []
        for row in sample_rows:
            if site_id and row.get("SITEID") != site_id:
                continue
            existing = reviewed.get(row.get("CANDIDATE_ID", ""))
            current_decision = existing["decision"] if existing else None
            recommended_decision = {"保留原文": "keep_original", "接受候选": "accept_candidate", "需要复核": "needs_review"}.get(row.get("AI_DECISION", ""), "needs_review")
            if ai_decision != "all" and recommended_decision != ai_decision:
                continue
            filtered.append({
                "sampleId": sample_id,
                "candidateId": row.get("CANDIDATE_ID", ""),
                "siteId": row.get("SITEID", ""),
                "assetNumber": row.get("ASSETNUM", ""),
                "originalDescription": row.get("ORIGINAL_DESCRIPTION", ""),
                "candidateDescription": row.get("EXISTING_CANDIDATE_DESCRIPTION", ""),
                "diffCategory": row.get("DIFF_CATEGORY", ""),
                "diffSignature": row.get("DIFF_SIGNATURE", ""),
                "kks": row.get("LOCATION_CODE", ""),
                "locationDescription": row.get("LOCATION_DESCRIPTION", ""),
                "locationParent": row.get("LOCATION_PARENT", ""),
                "classificationDescription": row.get("CLASSIFICATION_DESCRIPTION", ""),
                "confidence": row.get("CONFIDENCE", ""),
                "aiDecision": row.get("AI_DECISION", ""),
                "aiConfidence": row.get("AI_CONFIDENCE", ""),
                "aiReason": row.get("AI_REASON", ""),
                "decision": current_decision,
                "reviewNote": existing["note"] if existing else "",
                "reviewer": existing["reviewer"] if existing else "",
                "reviewedAt": existing["reviewed_at"] if existing else "",
            })
        start = (page - 1) * page_size
        return {"rows": filtered[start:start + page_size], "total": len(filtered), "page": page, "pageSize": page_size, "summary": summary}
    finally:
        sqlite.close()


@domain_post("/api/ai-review/decision")
def save_ai_review_decision(request: AiDecisionRequest, actor: str = Depends(require_decision_auth)) -> dict[str, Any]:
    sample_id, sample_rows = load_ai_review_sample()
    if request.sample_id != sample_id:
        raise HTTPException(status_code=409, detail="AI 样本版本已变化，请刷新页面")
    row = next((item for item in sample_rows if item.get("CANDIDATE_ID") == request.candidate_id), None)
    if row is None:
        raise HTTPException(status_code=404, detail="候选不在当前 AI 样本中")
    sqlite = sqlite_connection()
    try:
        now = utc_now()
        existing = sqlite.execute("SELECT * FROM ai_review_decision WHERE idempotency_key=?", (request.idempotency_key,)).fetchone()
        if existing is not None:
            return {"sampleId": sample_id, "candidateId": existing["candidate_id"], "decision": existing["decision"], "replayed": True}
        sqlite.execute(
            "INSERT INTO ai_review_decision(decision_id,sample_id,candidate_id,decision,note,reviewer,idempotency_key,reviewed_at) VALUES (?,?,?,?,?,?,?,?)",
            (f"ai-decision-{uuid.uuid4().hex}", sample_id, request.candidate_id, request.decision, request.note.strip(), actor, request.idempotency_key, now),
        )
        sqlite.execute(
            "INSERT INTO audit_event(entity_type,entity_id,event_type,actor,payload_json,event_at) VALUES (?,?,?,?,?,?)",
            ("ai_review", request.candidate_id, "ai_sample_decision_saved", actor, json.dumps({"sample_id": sample_id, "decision": request.decision, "source_write": False, "formal_publication": False}, ensure_ascii=False), now),
        )
        sqlite.commit()
        invalidate_ai_cluster_cache()
        return {"sampleId": sample_id, "candidateId": request.candidate_id, "decision": request.decision, "replayed": False}
    except sqlite3.IntegrityError as exc:
        sqlite.rollback()
        raise HTTPException(status_code=409, detail=f"AI 复核记录冲突: {exc}") from exc
    finally:
        sqlite.close()


@domain_post("/api/ai-review/bulk-decision")
def save_ai_bulk_decision(request: AiBulkDecisionRequest, actor: str = Depends(require_decision_auth)) -> dict[str, Any]:
    sample_id, sample_rows = load_ai_review_sample()
    if request.sample_id != sample_id:
        raise HTTPException(status_code=409, detail="AI 样本版本已变化，请刷新页面")
    targets = [row for row in sample_rows if row.get("AI_DECISION") == ("接受候选" if request.decision == "accept_candidate" else "保留原文")]
    sqlite = sqlite_connection()
    try:
        now = utc_now()
        applied = 0
        replayed = 0
        sqlite.execute("BEGIN IMMEDIATE")
        for row in targets:
            candidate_id = row["CANDIDATE_ID"]
            idempotency_key = f"{request.idempotency_key}-{candidate_id}"
            existing = sqlite.execute("SELECT decision FROM ai_review_decision WHERE candidate_id=?", (candidate_id,)).fetchone()
            if existing is not None:
                if existing["decision"] != request.decision:
                    raise HTTPException(status_code=409, detail=f"候选 {candidate_id} 已存在不同决策")
                replayed += 1
                continue
            sqlite.execute(
                "INSERT INTO ai_review_decision(decision_id,sample_id,candidate_id,decision,note,reviewer,idempotency_key,reviewed_at) VALUES (?,?,?,?,?,?,?,?)",
                (f"ai-decision-{uuid.uuid4().hex}", sample_id, candidate_id, request.decision, "批量确认 AI 建议", actor, idempotency_key, now),
            )
            sqlite.execute(
                "INSERT INTO audit_event(entity_type,entity_id,event_type,actor,payload_json,event_at) VALUES (?,?,?,?,?,?)",
                ("ai_review", candidate_id, "ai_sample_bulk_decision_saved", actor, json.dumps({"sample_id": sample_id, "decision": request.decision, "source_write": False, "formal_publication": False}, ensure_ascii=False), now),
            )
            applied += 1
        sqlite.commit()
        invalidate_ai_cluster_cache()
        return {"sampleId": sample_id, "decision": request.decision, "targetCount": len(targets), "appliedCount": applied, "replayedCount": replayed, "sourceWrite": False, "formalPublication": False}
    except HTTPException:
        sqlite.rollback()
        raise
    except sqlite3.IntegrityError as exc:
        sqlite.rollback()
        raise HTTPException(status_code=409, detail=f"AI 批量复核记录冲突: {exc}") from exc
    finally:
        sqlite.close()


@domain_get("/api/ai-review/clusters")
def ai_review_clusters(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=200),
    cluster_type: Literal["all", "question_context", "leading_minus", "terminal_hyphen", "other"] = "all",
    site_id: str | None = None,
    ai_decision: Literal["all", "accept_candidate", "keep_original", "needs_review"] = "all",
    decision: Literal["all", "pending", "accept_candidate", "keep_original", "needs_review"] = "all",
) -> dict[str, Any]:
    sqlite = sqlite_connection()
    try:
        batch_id = str(latest_batch(sqlite)["batch_id"])
        cached_summary = _AI_CLUSTER_SUMMARY_CACHE.get(batch_id)
        if cached_summary and time.monotonic() - cached_summary[0] < AI_CLUSTER_CACHE_TTL_SECONDS:
            clusters, summary = cached_summary[1], cached_summary[2]
        else:
            rows = load_ai_cluster_rows(sqlite)
            clusters, summary = summarize_ai_clusters(rows)
            _AI_CLUSTER_SUMMARY_CACHE[batch_id] = (time.monotonic(), clusters, summary)
        if cluster_type != "all":
            clusters = [item for item in clusters if item["clusterType"] == cluster_type]
        if site_id:
            clusters = [item for item in clusters if site_id in item["siteIds"]]
        if ai_decision != "all":
            clusters = [item for item in clusters if item["aiDecision"] == ai_decision]
        if decision == "pending":
            clusters = [item for item in clusters if item["decision"] is None]
        elif decision != "all":
            clusters = [item for item in clusters if item["decision"] == decision]
        start = (page - 1) * page_size
        return {
            "rows": clusters[start:start + page_size],
            "total": len(clusters),
            "page": page,
            "pageSize": page_size,
            "summary": summary,
            "filters": {"clusterType": cluster_type, "siteId": site_id or "", "aiDecision": ai_decision, "decision": decision},
        }
    finally:
        sqlite.close()


@domain_get("/api/ai-review/clusters/{cluster_id}")
def ai_review_cluster_detail(cluster_id: str, page: int = Query(default=1, ge=1), page_size: int = Query(default=50, ge=1, le=200)) -> dict[str, Any]:
    sqlite = sqlite_connection()
    try:
        rows = [row for row in load_ai_cluster_rows(sqlite) if row["clusterId"] == cluster_id]
        if not rows:
            raise HTTPException(status_code=404, detail="语义簇不存在或已不在当前待处理范围")
        clusters, _ = summarize_ai_clusters(rows)
        cluster = clusters[0]
        start = (page - 1) * page_size
        return {
            **cluster,
            "members": rows[start:start + page_size],
            "total": len(rows),
            "page": page,
            "pageSize": page_size,
        }
    finally:
        sqlite.close()


@domain_post("/api/ai-review/clusters/decision")
def save_ai_cluster_decision(request: AiClusterDecisionRequest, actor: str = Depends(require_decision_auth)) -> dict[str, Any]:
    sqlite = sqlite_connection()
    try:
        rows = [row for row in load_ai_cluster_rows(sqlite) if row["clusterId"] == request.cluster_id]
        if not rows:
            raise HTTPException(status_code=404, detail="语义簇不存在或已不在当前待处理范围")
        existing_by_key = sqlite.execute("SELECT * FROM ai_cluster_decision WHERE idempotency_key=?", (request.idempotency_key,)).fetchone()
        if existing_by_key is not None:
            return {
                "clusterId": request.cluster_id,
                "decision": existing_by_key["decision"],
                "memberCount": existing_by_key["member_count"],
                "appliedCount": existing_by_key["applied_count"],
                "replayedCount": existing_by_key["member_count"],
                "replayed": True,
                "sourceWrite": False,
                "formalPublication": False,
            }
        existing_cluster = sqlite.execute("SELECT * FROM ai_cluster_decision WHERE cluster_id=?", (request.cluster_id,)).fetchone()
        if existing_cluster is not None:
            if existing_cluster["decision"] != request.decision:
                raise HTTPException(status_code=409, detail="该语义簇已经记录了不同的整簇决策")
            return {
                "clusterId": request.cluster_id,
                "decision": existing_cluster["decision"],
                "memberCount": existing_cluster["member_count"],
                "appliedCount": existing_cluster["applied_count"],
                "replayedCount": existing_cluster["member_count"],
                "replayed": True,
                "sourceWrite": False,
                "formalPublication": False,
            }
        now = utc_now()
        applied = 0
        replayed = 0
        sqlite.execute("BEGIN IMMEDIATE")
        for row in rows:
            candidate_id = row["candidateId"]
            existing = sqlite.execute("SELECT decision FROM ai_review_decision WHERE candidate_id=?", (candidate_id,)).fetchone()
            if existing is not None:
                if existing["decision"] != request.decision:
                    raise HTTPException(status_code=409, detail=f"候选 {candidate_id} 已存在不同的 AI 决策")
                replayed += 1
                continue
            member_key = f"{request.idempotency_key}:{candidate_id}"
            sqlite.execute(
                "INSERT INTO ai_review_decision(decision_id,sample_id,candidate_id,decision,note,reviewer,idempotency_key,reviewed_at) VALUES (?,?,?,?,?,?,?,?)",
                (f"ai-cluster-member-{uuid.uuid4().hex}", f"cluster:{request.cluster_id}", candidate_id, request.decision, request.note.strip() or "整簇决策", actor, member_key, now),
            )
            applied += 1
        sqlite.execute(
            "INSERT INTO ai_cluster_decision(cluster_id,decision,member_count,applied_count,note,reviewer,idempotency_key,reviewed_at) VALUES (?,?,?,?,?,?,?,?)",
            (request.cluster_id, request.decision, len(rows), applied, request.note.strip(), actor, request.idempotency_key, now),
        )
        sqlite.execute(
            "INSERT INTO audit_event(entity_type,entity_id,event_type,actor,payload_json,event_at) VALUES (?,?,?,?,?,?)",
            ("ai_cluster", request.cluster_id, "ai_cluster_decision_saved", actor, json.dumps({"decision": request.decision, "member_count": len(rows), "applied_count": applied, "source_write": False, "formal_publication": False}, ensure_ascii=False), now),
        )
        sqlite.commit()
        invalidate_ai_cluster_cache()
        return {
            "clusterId": request.cluster_id,
            "decision": request.decision,
            "memberCount": len(rows),
            "appliedCount": applied,
            "replayedCount": replayed,
            "replayed": False,
            "sourceWrite": False,
            "formalPublication": False,
        }
    except HTTPException:
        sqlite.rollback()
        raise
    except sqlite3.IntegrityError as exc:
        sqlite.rollback()
        raise HTTPException(status_code=409, detail=f"语义簇决策记录冲突: {exc}") from exc
    finally:
        sqlite.close()


@domain_get("/api/candidates")
def candidates(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    search: str | None = None,
    site_id: str | None = None,
    classification: str | None = None,
    quick_filter: Literal["all", "pending", "context", "low", "deferred"] = "all",
    sample_only: bool = False,
    sample_size: int = Query(default=DEFAULT_SAMPLE_TARGET, ge=1, le=MAX_SAMPLE_TARGET),
) -> dict[str, Any]:
    sqlite = sqlite_connection()
    try:
        batch = latest_batch(sqlite)
        where = ["c.batch_id = ?"]
        parameters: list[Any] = [batch["batch_id"]]
        if sample_only:
            sample = ensure_review_sample(sqlite, batch, sample_size)
            where.append("EXISTS (SELECT 1 FROM review_sample_item si WHERE si.sample_id=? AND si.candidate_id=c.candidate_id)")
            parameters.append(sample["sample_id"])
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
        elif quick_filter == "deferred":
            where.append("c.review_state = 'deferred'")
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


@domain_get("/api/candidates/facets")
def candidate_facets(
    quick_filter: Literal["all", "pending", "context", "low", "deferred"] = "all",
    sample_only: bool = False,
    sample_size: int = Query(default=DEFAULT_SAMPLE_TARGET, ge=1, le=MAX_SAMPLE_TARGET),
) -> dict[str, Any]:
    """Return filter values from the current local batch, never hard-coded UI values."""
    sqlite = sqlite_connection()
    try:
        batch = latest_batch(sqlite)
        where = ["c.batch_id=?"]
        parameters: list[Any] = [batch["batch_id"]]
        if sample_only:
            sample = ensure_review_sample(sqlite, batch, sample_size)
            where.append("EXISTS (SELECT 1 FROM review_sample_item si WHERE si.sample_id=? AND si.candidate_id=c.candidate_id)")
            parameters.append(sample["sample_id"])
        if quick_filter == "pending":
            where.append("c.review_state='pending'")
        elif quick_filter == "deferred":
            where.append("c.review_state='deferred'")
        elif quick_filter == "low":
            where.append("c.confidence='low'")
        elif quick_filter == "context":
            where.append("(c.reason_codes_json LIKE '%LOCATION%' OR c.reason_codes_json LIKE '%CONTEXT%')")
        where_sql = " AND ".join(where)
        sites = sqlite.execute(
            f"SELECT COALESCE(NULLIF(trim(d.site_id),''),'未填写') AS value,count(*) AS count FROM semantic_candidate c JOIN device_identity d ON d.device_id=c.device_id WHERE {where_sql} GROUP BY value ORDER BY count DESC,value",
            parameters,
        ).fetchall()
        classifications = sqlite.execute(
            f"SELECT COALESCE(NULLIF(trim(d.classification_description),''),'未分类') AS value,count(*) AS count FROM semantic_candidate c JOIN device_identity d ON d.device_id=c.device_id WHERE {where_sql} GROUP BY value ORDER BY count DESC,value LIMIT 50",
            parameters,
        ).fetchall()
        return {
            "batchId": batch["batch_id"],
            "sites": [{"value": row["value"], "count": int(row["count"])} for row in sites],
            "classifications": [{"value": row["value"], "count": int(row["count"])} for row in classifications],
            "sourceWrite": False,
            "formalPublication": False,
        }
    finally:
        sqlite.close()


@domain_get("/api/candidates/{candidate_id}")
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
        return result
    finally:
        sqlite.close()


@domain_get("/api/formal-approval-queue")
def formal_approval_queue(
    status: Literal["all", "pending", "approved", "modified", "rejected", "deferred"] = "pending",
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    replay_id: str | None = None,
) -> dict[str, Any]:
    sqlite = sqlite_connection()
    try:
        where_parts: list[str] = []
        parameters: list[Any] = []
        if status != "all":
            where_parts.append("q.status=?")
            parameters.append(status)
        if replay_id:
            where_parts.append("q.replay_id=?")
            parameters.append(replay_id)
        where = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""
        total = sqlite.execute(f"SELECT count(*) FROM formal_approval_queue q {where}", parameters).fetchone()[0]
        rows = sqlite.execute(
            f"""
            SELECT q.queue_id,q.candidate_id,q.cluster_id,q.replay_id,q.proposed_decision,
              q.proposed_description,q.status,q.note,q.created_at,q.updated_at,
              c.batch_id,c.original_description,c.candidate_description,c.review_state,c.publication_state,
              d.source_snapshot_id,d.site_id,d.asset_number,d.source_asset_id,d.location_code,
              d.location_description,d.location_parent,d.classification_description,
              rr.status AS replay_status,rr.evaluation_count,rr.pass_count,rr.fail_count,
              r.review_id,r.approval_receipt,r.reviewer,r.reviewed_at
            FROM formal_approval_queue q
            JOIN semantic_candidate c ON c.candidate_id=q.candidate_id
            JOIN device_identity d ON d.device_id=c.device_id
            JOIN replay_run rr ON rr.replay_id=q.replay_id
            LEFT JOIN review_decision r ON r.candidate_id=q.candidate_id
            {where}
            ORDER BY q.created_at,q.queue_id
            LIMIT ? OFFSET ?
            """,
            parameters + [page_size, (page - 1) * page_size],
        ).fetchall()
        result = []
        for row in rows:
            result.append({
                "queueId": row["queue_id"],
                "candidateId": row["candidate_id"],
                "clusterId": row["cluster_id"],
                "replayId": row["replay_id"],
                "proposedDecision": row["proposed_decision"],
                "proposedDescription": row["proposed_description"],
                "status": row["status"],
                "note": row["note"],
                "createdAt": row["created_at"],
                "updatedAt": row["updated_at"],
                "batchId": row["batch_id"],
                "siteId": row["site_id"],
                "assetNumber": row["asset_number"],
                "assetId": row["source_asset_id"] or "",
                "originalDescription": row["original_description"],
                "candidateDescription": row["candidate_description"],
                "reviewState": row["review_state"],
                "publicationState": row["publication_state"],
                "kks": row["location_code"] or "",
                "locationDescription": row["location_description"] or "",
                "locationParent": row["location_parent"] or "",
                "classificationDescription": row["classification_description"] or "",
                "replayStatus": row["replay_status"],
                "replayEvaluationCount": row["evaluation_count"],
                "replayPassCount": row["pass_count"],
                "replayFailCount": row["fail_count"],
                "reviewId": row["review_id"] or "",
                "approvalReceipt": row["approval_receipt"] or "",
                "reviewer": row["reviewer"] or "",
                "reviewedAt": row["reviewed_at"] or "",
            })
        summary_where = ""
        summary_parameters: list[Any] = []
        if replay_id:
            summary_where = " WHERE replay_id=?"
            summary_parameters.append(replay_id)
        return {"rows": result, "total": int(total), "page": page, "pageSize": page_size, "summary": {"pending": int(sqlite.execute(f"SELECT count(*) FROM formal_approval_queue{summary_where} AND status='pending'" if summary_where else "SELECT count(*) FROM formal_approval_queue WHERE status='pending'", summary_parameters).fetchone()[0]), "completed": int(sqlite.execute(f"SELECT count(*) FROM formal_approval_queue{summary_where} AND status!='pending'" if summary_where else "SELECT count(*) FROM formal_approval_queue WHERE status!='pending'", summary_parameters).fetchone()[0])}}
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


@domain_post("/api/formal-approval-queue/batch-approve")
def formal_batch_approve(request: FormalBatchApprovalRequest, actor: str = Depends(require_decision_auth)) -> dict[str, Any]:
    """Approve every pending row in one replay batch, without publishing."""
    sqlite = sqlite_connection()
    try:
        prior = sqlite.execute(
            "SELECT entity_id,payload_json FROM audit_event WHERE event_type='formal_batch_approval_completed' ORDER BY event_id DESC LIMIT 200"
        ).fetchall()
        for event in prior:
            try:
                payload = json.loads(event["payload_json"])
            except json.JSONDecodeError:
                continue
            if payload.get("idempotency_key") == request.idempotency_key:
                return payload

        replay = sqlite.execute("SELECT * FROM replay_run WHERE replay_id=?", (request.replay_id,)).fetchone()
        if replay is None:
            raise HTTPException(status_code=404, detail="回放批次不存在")
        if replay["status"] != "passed" or int(replay["fail_count"]) != 0:
            raise HTTPException(status_code=409, detail="只有回放通过且无失败的批次才能批量审批")

        rows = sqlite.execute(
            """
            SELECT q.queue_id,q.candidate_id,q.proposed_description,q.status,
              c.batch_id,c.validator_status,c.review_state,c.publication_state
            FROM formal_approval_queue q
            JOIN semantic_candidate c ON c.candidate_id=q.candidate_id
            WHERE q.replay_id=? AND q.status='pending'
            ORDER BY q.queue_id
            """,
            (request.replay_id,),
        ).fetchall()
        if not rows:
            completed = sqlite.execute(
                "SELECT count(*) FROM formal_approval_queue WHERE replay_id=? AND status!='pending'",
                (request.replay_id,),
            ).fetchone()[0]
            return {
                "replayId": request.replay_id,
                "targetCount": 0,
                "appliedCount": 0,
                "pendingCount": 0,
                "completedCount": int(completed),
                "status": "already_completed",
                "idempotencyKey": request.idempotency_key,
                "sourceWrite": False,
                "formalPublication": False,
            }
        invalid = [
            row["candidate_id"]
            for row in rows
            if row["validator_status"] != "candidate"
            or row["review_state"] != "pending"
            or row["publication_state"] != "unpublished"
            or not str(row["proposed_description"] or "").strip()
        ]
        if invalid:
            raise HTTPException(status_code=409, detail=f"批次存在不满足审批门禁的记录：{len(invalid)} 条")

        now = utc_now()
        note = request.note.strip() or f"批次正式审批：回放 {replay['pass_count']}/{replay['evaluation_count']} 通过；审批后仍需独立发布。"
        sqlite.execute("BEGIN IMMEDIATE")
        receipts: list[str] = []
        for row in rows:
            review_id = f"review-{uuid.uuid4().hex}"
            receipt = f"receipt-{uuid.uuid4().hex}"
            sqlite.execute(
                """
                INSERT INTO review_decision
                  (review_id,candidate_id,decision,reviewed_description,reason_code,review_note,reviewer,approval_receipt,reviewed_at)
                VALUES (?,?,?,?,?,?,?,?,?)
                """,
                (review_id, row["candidate_id"], "modified", row["proposed_description"], "FORMAL_BATCH_APPROVED", note, actor, receipt, now),
            )
            sqlite.execute("UPDATE semantic_candidate SET review_state='modified' WHERE candidate_id=?", (row["candidate_id"],))
            sqlite.execute("UPDATE formal_approval_queue SET status='modified',note=?,updated_at=? WHERE candidate_id=?", (note, now, row["candidate_id"]))
            receipts.append(receipt)

        batch_ids = sorted({row["batch_id"] for row in rows})
        for batch_id in batch_ids:
            sqlite.execute(
                """
                UPDATE batch_run SET
                  needs_review_count=(SELECT count(*) FROM semantic_candidate WHERE batch_id=? AND review_state='pending'),
                  approved_count=(SELECT count(*) FROM semantic_candidate WHERE batch_id=? AND review_state IN ('approved','modified'))
                WHERE batch_id=?
                """,
                (batch_id, batch_id, batch_id),
            )
        # Keep the generic cleaning task state aligned with the formal queue
        # after approval.  Without this, the queue is complete but publication
        # still sees the task as pending approval.
        sync_cleaning_runs(sqlite, cleaning_registry_map(sqlite), now)
        payload = {
            "replayId": request.replay_id,
            "targetCount": len(rows),
            "appliedCount": len(rows),
            "pendingCount": 0,
            "completedCount": len(rows),
            "status": "completed",
            "idempotencyKey": request.idempotency_key,
            "approvalReceiptCount": len(receipts),
            "sourceWrite": False,
            "formalPublication": False,
        }
        sqlite.execute(
            "INSERT INTO audit_event(entity_type,entity_id,event_type,actor,payload_json,event_at) VALUES (?,?,?,?,?,?)",
            (
                "formal_approval_queue", f"batch-{request.replay_id}", "formal_batch_approval_completed", actor,
                json.dumps(payload, ensure_ascii=False), now,
            ),
        )
        sqlite.commit()
        return payload
    except HTTPException:
        if sqlite.in_transaction:
            sqlite.rollback()
        raise
    except sqlite3.IntegrityError as exc:
        if sqlite.in_transaction:
            sqlite.rollback()
        raise HTTPException(status_code=409, detail=f"批量审批写入冲突：{exc}") from exc
    finally:
        sqlite.close()


def cleaning_registry(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    return connection.execute(
        "SELECT rule_key,cleaning_type,rule_label,action_label,is_cleaning,replay_id,rule_version,enabled FROM cleaning_rule_registry WHERE enabled=1 ORDER BY is_cleaning DESC,rule_key"
    ).fetchall()


def cleaning_registry_map(connection: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    return {row["replay_id"]: dict(row) for row in cleaning_registry(connection)}


def sync_cleaning_runs(connection: sqlite3.Connection, registry: dict[str, dict[str, Any]], now: str | None = None) -> None:
    now = now or utc_now()
    if not registry:
        return
    replay_ids = tuple(registry)
    marks = ",".join("?" for _ in replay_ids)
    counts_by_replay = {
        row["replay_id"]: row
        for row in connection.execute(
            f"""
            SELECT q.replay_id,
              COUNT(*) total,
              SUM(CASE WHEN q.status='pending' THEN 1 ELSE 0 END) pending,
              SUM(CASE WHEN q.status!='pending' THEN 1 ELSE 0 END) approved,
              SUM(CASE WHEN c.publication_state='published' THEN 1 ELSE 0 END) published,
              MIN(c.batch_id) batch_id
            FROM formal_approval_queue q
            JOIN semantic_candidate c ON c.candidate_id=q.candidate_id
            WHERE q.replay_id IN ({marks})
            GROUP BY q.replay_id
            """,
            replay_ids,
        ).fetchall()
    }
    preview_by_replay = {
        row["replay_id"]: row
        for row in connection.execute(
            f"SELECT replay_id,preview_rows FROM cleaning_run WHERE replay_id IN ({marks})",
            replay_ids,
        ).fetchall()
    }
    replay_by_id = {
        row["replay_id"]: row
        for row in connection.execute(
            f"SELECT replay_id,status,fail_count FROM replay_run WHERE replay_id IN ({marks})",
            replay_ids,
        ).fetchall()
    }
    existing_by_replay = {
        row["replay_id"]: row
        for row in connection.execute(
            f"SELECT replay_id,status,stage,source_type FROM cleaning_run WHERE replay_id IN ({marks})",
            replay_ids,
        ).fetchall()
    }
    for replay_id, meta in registry.items():
        counts = counts_by_replay.get(replay_id)
        if counts is None:
            counts = {"total": 0, "pending": 0, "approved": 0, "published": 0, "batch_id": None}
        pending = int(counts["pending"] or 0)
        approved = int(counts["approved"] or 0)
        published = int(counts["published"] or 0)
        total = int(counts["total"] or 0)
        task_row = preview_by_replay.get(replay_id)
        preview_rows = int(task_row["preview_rows"] or 0) if task_row else 0
        replay = replay_by_id.get(replay_id)
        replay_status = replay["status"] if replay else None
        replay_fail_count = int(replay["fail_count"] or 0) if replay else 1
        existing_task = existing_by_replay.get(replay_id)
        is_approved_agent_task = bool(
            existing_task
            and existing_task["source_type"] == "rule_agent"
            and existing_task["stage"] in {"approved", "published"}
            and replay_status == "passed"
            and replay_fail_count == 0
        )
        status = "published" if total and published == total else "approved" if approved and pending == 0 else "pending_approval" if total else (existing_task["status"] if is_approved_agent_task else "draft")
        stage = (
            "published" if total and published == total else
            "approved" if approved and pending == 0 else
            "replayed" if replay_status == "passed" and replay_fail_count == 0 else
            "previewed" if total else
            (existing_task["stage"] if is_approved_agent_task else "task")
        )
        connection.execute(
            """
            UPDATE cleaning_run
            SET batch_id=?,status=?,stage=?,candidate_count=?,pending_count=?,approved_count=?,published_count=?,
                formal_publication=?,source_write=0,updated_at=?
            WHERE replay_id=?
            """,
            (
                counts["batch_id"],
                status,
                stage,
                total or preview_rows,
                pending,
                approved,
                published,
                1 if total and published == total else 0,
                now,
                replay_id,
            ),
        )


def cleaning_task_row(connection: sqlite3.Connection, task_id: str) -> sqlite3.Row:
    row = connection.execute(
        """
        SELECT u.*,r.cleaning_type,r.rule_label,r.action_label,r.is_cleaning,r.rule_version,r.enabled,
          rr.status AS replay_status,rr.evaluation_count,rr.pass_count,rr.fail_count
        FROM cleaning_run u
        JOIN cleaning_rule_registry r ON r.rule_key=u.rule_key AND r.replay_id=u.replay_id
        LEFT JOIN replay_run rr ON rr.replay_id=u.replay_id
            WHERE COALESCE(u.archived,0)=0
              AND (u.cleaning_run_id=? OR u.replay_id=? OR u.rule_key=?)
        LIMIT 1
        """,
        (task_id, task_id, task_id),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"清洗任务不存在：{task_id}")
    return row


def cleaning_task_next_action(row: sqlite3.Row) -> str:
    """Return the only legal next action for a task state."""
    return {
        "task": "preview",
        "previewed": "replay",
        "replayed": "approval",
        "approved": "publication",
        "published": "completed",
        "failed": "replay",
    }.get(str(row["stage"]), "blocked")


def cleaning_task_payload(row: sqlite3.Row) -> dict[str, Any]:
    next_action = cleaning_task_next_action(row)
    return {
        "taskId": row["cleaning_run_id"],
        "ruleKey": row["rule_key"],
        "ruleLabel": row["rule_label"],
        "cleaningType": row["cleaning_type"],
        "actionLabel": row["action_label"],
        "isCleaning": bool(row["is_cleaning"]),
        "replayId": row["replay_id"],
        "ruleVersion": row["rule_version"],
        "stage": row["stage"],
        "nextAction": next_action,
        "availableActions": [] if next_action in {"completed", "blocked"} else [next_action],
        "status": row["status"],
        "candidateCount": int(row["candidate_count"]),
        "pendingCount": int(row["pending_count"]),
        "approvedCount": int(row["approved_count"]),
        "publishedCount": int(row["published_count"]),
        "previewRows": int(row["preview_rows"] or 0),
        "replayRows": int(row["replay_rows"] or 0),
        "replayStatus": row["replay_status"],
        "replayEvaluationCount": int(row["evaluation_count"] or 0),
        "replayPassCount": int(row["pass_count"] or 0),
        "replayFailCount": int(row["fail_count"] or 0),
        "previewId": row["preview_id"],
        "previewSha256": row["preview_sha256"],
        "previewPath": row["preview_path"],
        "samplePath": row["sample_path"],
        "approvalIdempotencyKey": row["approval_idempotency_key"],
        "publicationRunId": row["publication_run_id"],
        "backupPath": row["backup_path"],
        "sourceWrite": bool(row["source_write"]),
        "formalPublication": bool(row["formal_publication"]),
        "lastError": row["last_error"],
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }


def hydrate_cleaning_preview(connection: sqlite3.Connection, row: sqlite3.Row) -> sqlite3.Row:
    """Backfill preview provenance from the existing immutable pilot artifacts once."""
    if row["preview_id"] and row["preview_sha256"]:
        preview_rows = int(row["preview_rows"] or 0)
        replay_rows = int(row["replay_rows"] or 0)
        preview_path = Path(row["preview_path"]) if row["preview_path"] else None
        if preview_rows == 0 and preview_path and preview_path.exists():
            with preview_path.open("r", encoding="utf-8-sig", newline="") as handle:
                preview_rows = sum(1 for _ in csv.DictReader(handle))
        if replay_rows == 0 and row["evaluation_count"]:
            replay_rows = int(row["evaluation_count"])
        if preview_rows != int(row["preview_rows"] or 0) or replay_rows != int(row["replay_rows"] or 0):
            connection.execute(
                "UPDATE cleaning_run SET preview_rows=?,replay_rows=?,updated_at=? WHERE cleaning_run_id=?",
                (preview_rows, replay_rows, utc_now(), row["cleaning_run_id"]),
            )
            connection.commit()
            return cleaning_task_row(connection, row["cleaning_run_id"])
        return row
    candidates = [
        CLEANING_TASK_DIR / row["rule_key"],
        PROJECT_ROOT / "pilots" / "HD_SAAS" / "question_separator_preview" if row["rule_key"] == "semantic.separator.fullwidth_question_mark_to_space" else None,
        PROJECT_ROOT / "pilots" / "HD_SAAS" / "terminal_hyphen_preview" if row["rule_key"] == "format.terminal_hyphen_trim" else None,
        PROJECT_ROOT / "pilots" / "HD_SAAS" / "ai_cluster_keep_original_replay" if row["rule_key"] == "policy.keep_original.leading_minus" else None,
    ]
    for directory in candidates:
        if directory is None:
            continue
        manifest = directory / "manifest.json"
        replay_manifest = directory / "replay_manifest.json"
        if not manifest.exists() and not replay_manifest.exists():
            continue
        payload: dict[str, Any] = {}
        for path in (manifest, replay_manifest):
            if path.exists():
                try:
                    payload.update(json.loads(path.read_text(encoding="utf-8")))
                except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                    continue
        preview_id = payload.get("preview_id") or payload.get("replay_id")
        preview_sha = payload.get("preview_sha256") or payload.get("replay_sha256")
        preview_file = payload.get("preview_file")
        sample_file = payload.get("sample_file")
        preview_count = payload.get("preview_rows") or payload.get("evaluation_count") or 0
        replay_count = payload.get("evaluation_count") or payload.get("preview_rows") or 0
        if preview_id and preview_sha:
            connection.execute(
                "UPDATE cleaning_run SET preview_id=?,preview_sha256=?,preview_path=?,sample_path=?,preview_rows=?,replay_rows=?,updated_at=? WHERE cleaning_run_id=?",
                (str(preview_id), str(preview_sha), preview_file, sample_file, int(preview_count), int(replay_count), utc_now(), row["cleaning_run_id"]),
            )
            connection.commit()
            return cleaning_task_row(connection, row["cleaning_run_id"])
    return row


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def generate_cleaning_preview(connection: sqlite3.Connection, row: sqlite3.Row, note: str = "") -> dict[str, Any]:
    """Build a task-scoped preview from the queued candidate rows only.

    This is deliberately read-only with respect to source and candidate content;
    it only records immutable preview provenance on cleaning_run.
    """
    queue_rows = connection.execute(
        """
        SELECT q.queue_id,q.candidate_id,q.proposed_description,q.status,
          c.original_description,c.candidate_description,c.review_state,c.publication_state,
          d.site_id,d.asset_number,d.source_asset_id,d.location_code,d.location_description,
          d.location_parent,d.classification_description
        FROM formal_approval_queue q
        JOIN semantic_candidate c ON c.candidate_id=q.candidate_id
        JOIN device_identity d ON d.device_id=c.device_id
        WHERE q.replay_id=?
        ORDER BY d.site_id,d.asset_number,q.candidate_id
        """,
        (row["replay_id"],),
    ).fetchall()
    if not queue_rows:
        raise HTTPException(status_code=409, detail="任务没有可生成预览的候选记录")

    output_dir = CLEANING_TASK_DIR / re.sub(r"[^A-Za-z0-9_.-]+", "_", row["rule_key"])
    output_dir.mkdir(parents=True, exist_ok=True)
    preview_id = f"preview-{row['cleaning_run_id']}-{hashlib.sha256('|'.join(item['candidate_id'] for item in queue_rows).encode('utf-8')).hexdigest()[:16]}"
    preview_path = output_dir / "preview.csv"
    sample_path = output_dir / "sample.csv"
    fieldnames = ["TASK_ID", "RULE_KEY", "RULE_VERSION", "QUEUE_ID", "CANDIDATE_ID", "SITEID", "ASSETNUM", "ASSETID", "ORIGINAL_DESCRIPTION", "CANDIDATE_DESCRIPTION", "PROPOSED_DESCRIPTION", "STATUS", "REVIEW_STATE", "PUBLICATION_STATE", "LOCATION_CODE", "LOCATION_DESCRIPTION", "LOCATION_PARENT", "CLASSIFICATION_DESCRIPTION"]

    def as_dict(item: sqlite3.Row) -> dict[str, Any]:
        return {
            "TASK_ID": row["cleaning_run_id"],
            "RULE_KEY": row["rule_key"],
            "RULE_VERSION": row["rule_version"],
            "QUEUE_ID": item["queue_id"],
            "CANDIDATE_ID": item["candidate_id"],
            "SITEID": item["site_id"] or "",
            "ASSETNUM": item["asset_number"] or "",
            "ASSETID": item["source_asset_id"] or "",
            "ORIGINAL_DESCRIPTION": item["original_description"] or "",
            "CANDIDATE_DESCRIPTION": item["candidate_description"] or "",
            "PROPOSED_DESCRIPTION": item["proposed_description"] or "",
            "STATUS": item["status"],
            "REVIEW_STATE": item["review_state"],
            "PUBLICATION_STATE": item["publication_state"],
            "LOCATION_CODE": item["location_code"] or "",
            "LOCATION_DESCRIPTION": item["location_description"] or "",
            "LOCATION_PARENT": item["location_parent"] or "",
            "CLASSIFICATION_DESCRIPTION": item["classification_description"] or "",
        }

    preview_rows = [as_dict(item) for item in queue_rows]
    for path, items in ((preview_path, preview_rows), (sample_path, preview_rows[: min(200, len(preview_rows))])):
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(items)
    now = utc_now()
    preview_sha256 = sha256_file(preview_path)
    connection.execute(
        """
        UPDATE cleaning_run
        SET stage=CASE WHEN stage='published' THEN stage ELSE 'previewed' END,
            preview_id=?,preview_sha256=?,preview_path=?,sample_path=?,preview_rows=?,last_error=NULL,updated_at=?
        WHERE cleaning_run_id=?
        """,
        (preview_id, preview_sha256, str(preview_path), str(sample_path), len(preview_rows), now, row["cleaning_run_id"]),
    )
    payload = {"taskId": row["cleaning_run_id"], "stage": row["stage"] if row["stage"] == "published" else "previewed", "previewId": preview_id, "previewSha256": preview_sha256, "previewRows": len(preview_rows), "sampleRows": min(200, len(preview_rows)), "previewPath": str(preview_path), "samplePath": str(sample_path), "sourceWrite": False, "formalPublication": False}
    connection.execute(
        "INSERT INTO audit_event(entity_type,entity_id,event_type,actor,payload_json,event_at) VALUES (?,?,?,?,?,?)",
        ("cleaning_task", row["cleaning_run_id"], "cleaning_preview_generated", "local-user", json.dumps({**payload, "note": note}, ensure_ascii=False), now),
    )
    connection.commit()
    return payload


def expected_cleaning_description(cleaning_type: str, original: str, proposed: str) -> str:
    if cleaning_type == "separator":
        return original.replace("？", " ")
    if cleaning_type == "terminal_hyphen":
        return original[:-1] if original.endswith("-") else original
    if cleaning_type == "keep_original":
        return original
    if cleaning_type in {"space", "whitespace"}:
        return re.sub(r"[\s\u3000]+", " ", original).strip()
    return proposed


def execute_cleaning_replay(connection: sqlite3.Connection, row: sqlite3.Row, note: str = "") -> dict[str, Any]:
    row = hydrate_cleaning_preview(connection, row)
    if not row["preview_path"] or not Path(row["preview_path"]).exists():
        generate_cleaning_preview(connection, row, note)
        row = cleaning_task_row(connection, row["cleaning_run_id"])
    preview_path = Path(row["preview_path"])
    with preview_path.open("r", encoding="utf-8-sig", newline="") as handle:
        preview_rows = list(csv.DictReader(handle))
    if not preview_rows:
        raise HTTPException(status_code=409, detail="预览为空，不能回放")
    ids = [item.get("CANDIDATE_ID", "") for item in preview_rows]
    if any(not item for item in ids) or len(set(ids)) != len(ids):
        raise HTTPException(status_code=409, detail="预览存在空的或重复的候选身份")
    placeholders = ",".join("?" for _ in ids)
    db_rows = connection.execute(
        f"""
        SELECT q.candidate_id,q.proposed_decision,q.proposed_description,
          c.original_description,c.candidate_description,c.review_state,c.publication_state,
          d.source_snapshot_id,d.source_schema,d.site_id,d.asset_number,d.location_code,
          d.location_description,d.location_parent,d.classification_description
        FROM formal_approval_queue q
        JOIN semantic_candidate c ON c.candidate_id=q.candidate_id
        JOIN device_identity d ON d.device_id=c.device_id
        WHERE q.replay_id=? AND q.candidate_id IN ({placeholders})
        """,
        (row["replay_id"], *ids),
    ).fetchall()
    by_id = {item["candidate_id"]: item for item in db_rows}
    failures: list[dict[str, str]] = []
    results: list[tuple[str, sqlite3.Row, dict[str, str], str]] = []
    for item in preview_rows:
        candidate_id = item["CANDIDATE_ID"]
        db_row = by_id.get(candidate_id)
        if db_row is None:
            failures.append({"candidateId": candidate_id, "reason": "candidate_missing_or_wrong_task"})
            continue
        checks = {
            "original_matches": item.get("ORIGINAL_DESCRIPTION", "") == (db_row["original_description"] or ""),
            "proposed_matches": item.get("PROPOSED_DESCRIPTION", "") == (db_row["proposed_description"] or ""),
            "identity_matches": item.get("SITEID", "") == (db_row["site_id"] or "") and item.get("ASSETNUM", "") == (db_row["asset_number"] or ""),
            "description_nonempty": bool(str(item.get("PROPOSED_DESCRIPTION", "")).strip()),
            "rule_transformation_matches": item.get("PROPOSED_DESCRIPTION", "") == expected_cleaning_description(row["cleaning_type"], db_row["original_description"] or "", db_row["proposed_description"] or ""),
        }
        for check, passed in checks.items():
            if not passed:
                failures.append({"candidateId": candidate_id, "reason": check})
        results.append((candidate_id, db_row, item, "modified" if row["is_cleaning"] else "approved"))
    if len(db_rows) != len(preview_rows):
        failures.append({"candidateId": "(scope)", "reason": "preview_db_count_mismatch"})
    replay_id = row["replay_id"]
    now = utc_now()
    existing = connection.execute("SELECT * FROM replay_run WHERE replay_id=?", (replay_id,)).fetchone()
    passed = len(results) - sum(1 for item in failures if item["candidateId"] != "(scope)")
    failed = len(preview_rows) - passed
    if any(item["candidateId"] == "(scope)" for item in failures):
        failed = max(failed, 1)
    status = "passed" if not failures else "failed"
    if existing is None:
        connection.execute(
            "INSERT INTO replay_run(replay_id,rule_version,validator_version,evaluation_count,pass_count,fail_count,status,started_at,finished_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (replay_id, row["rule_version"], "cleaning-task-replay-v1", len(preview_rows), passed, failed, status, now, utc_now()),
        )
        for candidate_id, db_row, item, decision in results:
            case_id = f"case-{replay_id}-{candidate_id}"
            connection.execute(
                "INSERT OR IGNORE INTO evaluation_case(case_id,source_review_id,source_snapshot_id,source_schema,site_id,asset_number,input_description,context_json,expected_decision,expected_description,failure_type,active,introduced_rule_version,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (case_id, None, db_row["source_snapshot_id"], db_row["source_schema"], db_row["site_id"], db_row["asset_number"], db_row["original_description"], json.dumps({"task_id": row["cleaning_run_id"], "rule_key": row["rule_key"], "source_write": False, "formal_publication": False}, ensure_ascii=False), decision, item["PROPOSED_DESCRIPTION"], row["cleaning_type"], 1, row["rule_version"], now),
            )
            connection.execute(
                "INSERT INTO replay_result(replay_id,case_id,actual_decision,actual_description,outcome,message) VALUES (?,?,?,?,?,?)",
                (replay_id, case_id, decision, item["PROPOSED_DESCRIPTION"], "pass" if not any(f["candidateId"] == candidate_id for f in failures) else "fail", None),
            )
        connection.execute(
            "INSERT INTO audit_event(entity_type,entity_id,event_type,actor,payload_json,event_at) VALUES (?,?,?,?,?,?)",
            ("cleaning_task", row["cleaning_run_id"], "cleaning_replay_executed", "local-user", json.dumps({"replayId": replay_id, "previewId": row["preview_id"], "evaluationCount": len(preview_rows), "passCount": passed, "failCount": failed, "sourceWrite": False, "formalPublication": False, "note": note}, ensure_ascii=False), now),
        )
    else:
        status = existing["status"]
        passed = int(existing["pass_count"])
        failed = int(existing["fail_count"])
    connection.execute("UPDATE cleaning_run SET stage=CASE WHEN stage='published' THEN stage ELSE ? END,replay_rows=?,last_error=?,updated_at=? WHERE cleaning_run_id=?", ("replayed" if status == "passed" else "failed", len(preview_rows), None if status == "passed" else "回放校验失败", now, row["cleaning_run_id"]))
    connection.commit()
    return {"taskId": row["cleaning_run_id"], "stage": "published" if row["stage"] == "published" else ("replayed" if status == "passed" else "failed"), "replayId": replay_id, "evaluationCount": len(preview_rows), "passCount": passed, "failCount": failed, "status": status, "failures": failures, "sourceWrite": False, "formalPublication": False}


def cleaning_task_replay_ids(connection: sqlite3.Connection, task_id: str | None) -> tuple[str, ...]:
    registry = cleaning_registry(connection)
    if not task_id:
        return tuple(row["replay_id"] for row in registry if row["is_cleaning"])
    row = cleaning_task_row(connection, task_id)
    if not row["is_cleaning"]:
        raise HTTPException(status_code=409, detail="该任务是保留原文策略，不属于清洗发布范围")
    return (row["replay_id"],)


@domain_get("/api/cleaning/tasks")
def cleaning_tasks() -> dict[str, Any]:
    """Return the single task model used by preview, replay, approval and publication."""
    sqlite = sqlite_connection()
    try:
        registry = cleaning_registry_map(sqlite)
        sync_cleaning_runs(sqlite, registry)
        sqlite.commit()
        rows = sqlite.execute(
            """
            SELECT u.*,r.cleaning_type,r.rule_label,r.action_label,r.is_cleaning,r.rule_version,r.enabled,
              rr.status AS replay_status,rr.evaluation_count,rr.pass_count,rr.fail_count
            FROM cleaning_run u
            JOIN cleaning_rule_registry r ON r.rule_key=u.rule_key AND r.replay_id=u.replay_id
            LEFT JOIN replay_run rr ON rr.replay_id=u.replay_id
            WHERE r.enabled=1
            ORDER BY u.created_at,u.cleaning_run_id
            """
        ).fetchall()
        rows = [hydrate_cleaning_preview(sqlite, row) for row in rows]
        return {
            "tasks": [cleaning_task_payload(row) for row in rows],
            "workflow": ["preview", "replay", "approval", "publication"],
            "sourceWrite": False,
        }
    finally:
        sqlite.close()


@domain_get("/api/cleaning/tasks/{task_id}")
def cleaning_task(task_id: str) -> dict[str, Any]:
    sqlite = sqlite_connection()
    try:
        registry = cleaning_registry_map(sqlite)
        sync_cleaning_runs(sqlite, registry)
        sqlite.commit()
        return {"task": cleaning_task_payload(hydrate_cleaning_preview(sqlite, cleaning_task_row(sqlite, task_id))), "sourceWrite": False}
    finally:
        sqlite.close()


@domain_post("/api/cleaning/tasks/{task_id}/preview")
def record_cleaning_preview(task_id: str, request: CleaningTaskPreviewRequest) -> dict[str, Any]:
    """Register a generated preview without changing candidate or source rows."""
    sqlite = sqlite_connection()
    try:
        row = cleaning_task_row(sqlite, task_id)
        if row["stage"] == "published":
            raise HTTPException(status_code=409, detail="已发布任务不能重新生成预览")
        if not request.preview_id or not request.preview_sha256:
            return generate_cleaning_preview(sqlite, row, request.note)
        now = utc_now()
        sqlite.execute(
            """
            UPDATE cleaning_run
            SET stage='previewed',preview_id=?,preview_sha256=?,preview_path=?,sample_path=?,last_error=NULL,updated_at=?
            WHERE cleaning_run_id=?
            """,
            (request.preview_id, request.preview_sha256, request.preview_path, request.sample_path, now, row["cleaning_run_id"]),
        )
        payload = {"taskId": row["cleaning_run_id"], "stage": "previewed", "previewId": request.preview_id, "previewSha256": request.preview_sha256, "sourceWrite": False}
        sqlite.execute(
            "INSERT INTO audit_event(entity_type,entity_id,event_type,actor,payload_json,event_at) VALUES (?,?,?,?,?,?)",
            ("cleaning_task", row["cleaning_run_id"], "cleaning_preview_recorded", "local-user", json.dumps({**payload, "note": request.note}, ensure_ascii=False), now),
        )
        sqlite.commit()
        return payload
    finally:
        sqlite.close()


@domain_post("/api/cleaning/tasks/{task_id}/replay")
def gate_cleaning_replay(task_id: str, request: CleaningTaskActionRequest) -> dict[str, Any]:
    """Run the task-scoped deterministic replay and open the approval gate."""
    sqlite = sqlite_connection()
    try:
        row = cleaning_task_row(sqlite, task_id)
        payload = execute_cleaning_replay(sqlite, row, request.note)
        payload["idempotencyKey"] = request.idempotency_key
        return payload
    finally:
        sqlite.close()


@domain_post("/api/cleaning/tasks/{task_id}/advance")
def advance_cleaning_task(task_id: str, request: CleaningTaskAdvanceRequest, actor: str = Depends(require_decision_auth)) -> dict[str, Any]:
    """Advance a task once through the common preview/replay/approval/publication state machine."""
    sqlite = sqlite_connection()
    try:
        prior = sqlite.execute(
            "SELECT payload_json FROM audit_event WHERE entity_type='cleaning_task' AND entity_id=? AND event_type='cleaning_task_advanced' ORDER BY event_id DESC LIMIT 100",
            (task_id,),
        ).fetchall()
        for event in prior:
            try:
                payload = json.loads(event["payload_json"])
            except json.JSONDecodeError:
                continue
            if payload.get("idempotencyKey") == request.idempotency_key:
                payload.pop("note", None)
                return payload

        row = cleaning_task_row(sqlite, task_id)
        if not row["is_cleaning"]:
            raise HTTPException(status_code=409, detail="该任务是保留原文策略，不属于清洗任务推进范围")

        action = cleaning_task_next_action(row)
        if action == "completed":
            payload: dict[str, Any] = {
                "taskId": row["cleaning_run_id"],
                "action": "completed",
                "stage": "published",
                "status": "already_completed",
                "targetCount": int(row["candidate_count"]),
                "publishedCount": int(row["published_count"]),
                "idempotencyKey": request.idempotency_key,
                "sourceWrite": False,
                "formalPublication": True,
            }
        elif action == "blocked":
            raise HTTPException(status_code=409, detail=f"任务当前状态不可推进：{row['stage']}")
        elif action == "publication" and not request.confirm_publication:
            payload = {
                "taskId": row["cleaning_run_id"],
                "action": "publication",
                "stage": row["stage"],
                "status": "confirmation_required",
                "targetCount": int(row["candidate_count"]),
                "requiresConfirmation": True,
                "idempotencyKey": request.idempotency_key,
                "sourceWrite": False,
                "formalPublication": False,
            }
        elif action == "preview":
            payload = generate_cleaning_preview(sqlite, row, request.note)
        elif action == "replay":
            payload = execute_cleaning_replay(sqlite, row, request.note)
        elif action == "approval":
            payload = cleaning_batch_approve(
                CleaningBatchApprovalRequest(
                    scope="cleaning",
                    task_id=task_id,
                    note=request.note,
                    idempotency_key=request.idempotency_key,
                ),
                actor=actor,
            )
        elif action == "publication":
            payload = cleaning_publish(
                CleaningBatchPublishRequest(
                    scope="cleaning",
                    task_id=task_id,
                    note=request.note,
                    idempotency_key=request.idempotency_key,
                ),
                actor=actor,
            )
        else:
            raise HTTPException(status_code=409, detail=f"任务没有可执行动作：{action}")

        refreshed = cleaning_task_row(sqlite, task_id)
        payload = {
            **payload,
            "taskId": refreshed["cleaning_run_id"],
            "action": payload.get("action", action),
            "stage": refreshed["stage"],
            "nextAction": cleaning_task_next_action(refreshed),
            "idempotencyKey": request.idempotency_key,
            "sourceWrite": False,
            "formalPublication": bool(payload.get("formalPublication", False)),
        }
        if payload.get("requiresConfirmation"):
            return payload
        now = utc_now()
        sqlite.execute(
            "INSERT INTO audit_event(entity_type,entity_id,event_type,actor,payload_json,event_at) VALUES (?,?,?,?,?,?)",
            ("cleaning_task", task_id, "cleaning_task_advanced", actor, json.dumps(payload, ensure_ascii=False), now),
        )
        sqlite.commit()
        return payload
    except HTTPException:
        if sqlite.in_transaction:
            sqlite.rollback()
        raise
    finally:
        sqlite.close()


@domain_get("/api/cleaning/rules")
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


@domain_post("/api/cleaning/rules")
def register_cleaning_rule(request: CleaningRuleRegistrationRequest) -> dict[str, Any]:
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
            ("cleaning_rule", request.rule_key, "cleaning_rule_registered", "local-user", json.dumps(request.model_dump(by_alias=True), ensure_ascii=False), now),
        )
        sqlite.commit()
        return {"ruleKey": request.rule_key, "replayId": request.replay_id, "status": "registered"}
    except sqlite3.IntegrityError as exc:
        sqlite.rollback()
        raise HTTPException(status_code=409, detail=f"清洗规则已存在或 replay_id 冲突：{exc}") from exc
    finally:
        sqlite.close()


@domain_get("/api/cleaning")
def cleaning_queue(
    scope: Literal["cleaning", "keep_original", "all"] = "cleaning",
    status: Literal["all", "pending", "approved", "modified", "rejected", "deferred"] = "pending",
    task_id: str | None = Query(default=None, alias="task_id"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    """One compact workbench for deterministic cleaning batches."""
    sqlite = sqlite_connection()
    try:
        registry = cleaning_registry_map(sqlite)
        replay_ids = cleaning_task_replay_ids(sqlite, task_id) if task_id else tuple(registry)
        if not replay_ids:
            return {"rows": [], "total": 0, "page": page, "pageSize": page_size, "summary": {"totalPending": 0, "cleaningPending": 0, "cleaningPublished": 0, "keepOriginalPending": 0, "rules": []}}
        sync_registry = registry if not task_id else {replay_id: registry[replay_id] for replay_id in replay_ids if replay_id in registry}
        sync_cleaning_runs(sqlite, sync_registry)
        sqlite.commit()
        scoped_replay_ids = tuple(
            replay_id
            for replay_id in replay_ids
            if (scope == "all"
                or (scope == "cleaning" and registry.get(replay_id, {}).get("is_cleaning"))
                or (scope == "keep_original" and not registry.get(replay_id, {}).get("is_cleaning")))
        )
        if not scoped_replay_ids:
            return {"rows": [], "total": 0, "page": page, "pageSize": page_size, "summary": {"totalPending": 0, "cleaningPending": 0, "cleaningPublished": 0, "keepOriginalPending": 0, "rules": []}}
        replay_marks = ",".join("?" for _ in scoped_replay_ids)
        where_parts = [
            f"q.replay_id IN ({replay_marks})",
            "EXISTS (SELECT 1 FROM cleaning_run u WHERE u.replay_id=q.replay_id AND COALESCE(u.archived,0)=0)",
        ]
        where_parameters: list[Any] = list(scoped_replay_ids)
        if status != "all":
            where_parts.append("q.status=?")
            where_parameters.append(status)
        where_sql = " AND ".join(where_parts)
        total = int(sqlite.execute(f"SELECT count(*) FROM formal_approval_queue q WHERE {where_sql}", where_parameters).fetchone()[0])
        rows = sqlite.execute(
            f"""
            SELECT q.queue_id,q.candidate_id,q.cluster_id,q.replay_id,q.proposed_decision,
              q.proposed_description,q.status,q.note,q.created_at,q.updated_at,
              c.batch_id,c.original_description,c.candidate_description,c.review_state,c.publication_state,
              d.source_snapshot_id,d.site_id,d.asset_number,d.source_asset_id,d.location_code,
              d.location_description,d.location_parent,d.classification_description,
              rr.status AS replay_status,rr.evaluation_count,rr.pass_count,rr.fail_count,
              r.review_id,r.approval_receipt,r.reviewer,r.reviewed_at
            FROM formal_approval_queue q
            JOIN semantic_candidate c ON c.candidate_id=q.candidate_id
            JOIN device_identity d ON d.device_id=c.device_id
            JOIN replay_run rr ON rr.replay_id=q.replay_id
            LEFT JOIN review_decision r ON r.candidate_id=q.candidate_id
            WHERE {where_sql}
            ORDER BY q.created_at,q.queue_id
            LIMIT ? OFFSET ?
            """,
            where_parameters + [page_size, (page - 1) * page_size],
        ).fetchall()
        mapped: list[dict[str, Any]] = []
        for row in rows:
            meta = registry.get(row["replay_id"], {"cleaning_type": "other", "rule_key": "other", "rule_label": "其他待确认", "action_label": "待确认", "is_cleaning": 0})
            mapped.append({
                "queueId": row["queue_id"],
                "candidateId": row["candidate_id"],
                "clusterId": row["cluster_id"],
                "replayId": row["replay_id"],
                "cleaningType": meta["cleaning_type"],
                "ruleKey": meta["rule_key"],
                "ruleLabel": meta["rule_label"],
                "actionLabel": meta["action_label"],
                "proposedDecision": row["proposed_decision"],
                "proposedDescription": row["proposed_description"],
                "status": row["status"],
                "note": row["note"],
                "createdAt": row["created_at"],
                "updatedAt": row["updated_at"],
                "batchId": row["batch_id"],
                "siteId": row["site_id"],
                "assetNumber": row["asset_number"],
                "assetId": row["source_asset_id"] or "",
                "originalDescription": row["original_description"],
                "candidateDescription": row["candidate_description"],
                "reviewState": row["review_state"],
                "publicationState": row["publication_state"],
                "kks": row["location_code"] or "",
                "locationDescription": row["location_description"] or "",
                "locationParent": row["location_parent"] or "",
                "classificationDescription": row["classification_description"] or "",
                "replayStatus": row["replay_status"],
                "replayEvaluationCount": row["evaluation_count"],
                "replayPassCount": row["pass_count"],
                "replayFailCount": row["fail_count"],
                "reviewId": row["review_id"] or "",
                "approvalReceipt": row["approval_receipt"] or "",
                "reviewer": row["reviewer"] or "",
                "reviewedAt": row["reviewed_at"] or "",
            })
        summary_rows = sqlite.execute(
            f"""
            SELECT q.replay_id,
              COUNT(*) total,
              SUM(CASE WHEN q.status='pending' THEN 1 ELSE 0 END) pending,
              SUM(CASE WHEN q.status!='pending' THEN 1 ELSE 0 END) completed,
              SUM(CASE WHEN c.publication_state='published' THEN 1 ELSE 0 END) published
            FROM formal_approval_queue q
            JOIN semantic_candidate c ON c.candidate_id=q.candidate_id
             WHERE q.replay_id IN ({replay_marks})
               AND EXISTS (SELECT 1 FROM cleaning_run u WHERE u.replay_id=q.replay_id AND COALESCE(u.archived,0)=0)
            GROUP BY q.replay_id
            """,
            scoped_replay_ids,
        ).fetchall()
        total_pending = sum(int(row["pending"] or 0) for row in summary_rows)
        cleaning_pending = sum(int(row["pending"] or 0) for row in summary_rows if registry.get(row["replay_id"], {}).get("is_cleaning"))
        keep_original_pending = total_pending - cleaning_pending
        cleaning_published = sum(int(row["published"] or 0) for row in summary_rows if registry.get(row["replay_id"], {}).get("is_cleaning"))
        by_rule: dict[str, dict[str, Any]] = {}
        for row in summary_rows:
            meta = registry.get(row["replay_id"], {"rule_key": "other", "rule_label": "其他待确认", "is_cleaning": 0})
            key = str(meta["rule_key"])
            item = by_rule.setdefault(key, {"ruleKey": key, "ruleLabel": meta["rule_label"], "total": 0, "pending": 0, "completed": 0, "cleaning": bool(meta["is_cleaning"])})
            item["total"] += int(row["total"] or 0)
            item["pending"] += int(row["pending"] or 0)
            item["completed"] += int(row["completed"] or 0)
        return {
            "rows": mapped,
            "total": total,
            "page": page,
            "pageSize": page_size,
            "summary": {
                "totalPending": total_pending,
                "cleaningPending": cleaning_pending,
                "cleaningPublished": cleaning_published,
                "keepOriginalPending": keep_original_pending,
                "rules": list(by_rule.values()),
            },
        }
    finally:
        sqlite.close()


@domain_post("/api/cleaning/batch-approve")
def cleaning_batch_approve(request: CleaningBatchApprovalRequest, actor: str = Depends(require_decision_auth)) -> dict[str, Any]:
    """Approve all replay-passed deterministic cleaning rows as one operation."""
    sqlite = sqlite_connection()
    try:
        prior = sqlite.execute(
            "SELECT payload_json FROM audit_event WHERE event_type='cleaning_batch_approval_completed' ORDER BY event_id DESC LIMIT 100"
        ).fetchall()
        for event in prior:
            try:
                payload = json.loads(event["payload_json"])
            except json.JSONDecodeError:
                continue
            if payload.get("idempotencyKey") == request.idempotency_key:
                return payload

        registry = cleaning_registry_map(sqlite)
        replay_ids = cleaning_task_replay_ids(sqlite, request.task_id)
        if not replay_ids:
            return {"targetCount": 0, "appliedCount": 0, "pendingCount": 0, "completedCount": 0, "status": "empty", "idempotencyKey": request.idempotency_key, "sourceWrite": False, "formalPublication": False}
        if request.task_id:
            task = cleaning_task_row(sqlite, request.task_id)
            if task["stage"] == "published":
                return {"targetCount": int(task["candidate_count"]), "appliedCount": 0, "pendingCount": int(task["pending_count"]), "completedCount": int(task["approved_count"]), "status": "already_completed", "idempotencyKey": request.idempotency_key, "taskId": task["cleaning_run_id"], "sourceWrite": False, "formalPublication": True}
            if task["stage"] not in {"replayed", "previewed"}:
                raise HTTPException(status_code=409, detail="任务尚未通过回放，不能审批")
        marks = ",".join("?" for _ in replay_ids)
        rows = sqlite.execute(
            f"""
            SELECT q.candidate_id,q.proposed_description,c.batch_id,c.validator_status,c.review_state,c.publication_state,
              rr.status AS replay_status,rr.fail_count
            FROM formal_approval_queue q
            JOIN semantic_candidate c ON c.candidate_id=q.candidate_id
            JOIN replay_run rr ON rr.replay_id=q.replay_id
            WHERE q.replay_id IN ({marks}) AND q.status='pending'
            ORDER BY q.replay_id,q.candidate_id
            """,
            replay_ids,
        ).fetchall()
        if not rows:
            completed = sqlite.execute(
                f"SELECT count(*) FROM formal_approval_queue WHERE replay_id IN ({marks}) AND status!='pending'",
                replay_ids,
            ).fetchone()[0]
            return {"targetCount": 0, "appliedCount": 0, "pendingCount": 0, "completedCount": int(completed), "status": "already_completed", "idempotencyKey": request.idempotency_key, "sourceWrite": False, "formalPublication": False}
        invalid = [row["candidate_id"] for row in rows if row["replay_status"] != "passed" or int(row["fail_count"]) != 0 or row["validator_status"] != "candidate" or row["review_state"] != "pending" or row["publication_state"] != "unpublished" or not str(row["proposed_description"] or "").strip()]
        if invalid:
            raise HTTPException(status_code=409, detail=f"清洗批次存在不满足审批门禁的记录：{len(invalid)} 条")

        now = utc_now()
        note = request.note.strip() or "批量清洗确认：仅处理回放通过的确定性规则；审批后仍需独立发布。"
        sqlite.execute("BEGIN IMMEDIATE")
        for row in rows:
            review_id = f"review-{uuid.uuid4().hex}"
            receipt = f"receipt-{uuid.uuid4().hex}"
            sqlite.execute(
                "INSERT INTO review_decision(review_id,candidate_id,decision,reviewed_description,reason_code,review_note,reviewer,approval_receipt,reviewed_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (review_id, row["candidate_id"], "modified", row["proposed_description"], "CLEANING_BATCH_APPROVED", note, actor, receipt, now),
            )
            sqlite.execute("UPDATE semantic_candidate SET review_state='modified' WHERE candidate_id=?", (row["candidate_id"],))
            sqlite.execute("UPDATE formal_approval_queue SET status='modified',note=?,updated_at=? WHERE candidate_id=?", (note, now, row["candidate_id"]))
        for batch_id in sorted({row["batch_id"] for row in rows}):
            sqlite.execute(
                "UPDATE batch_run SET needs_review_count=(SELECT count(*) FROM semantic_candidate WHERE batch_id=? AND review_state='pending'), approved_count=(SELECT count(*) FROM semantic_candidate WHERE batch_id=? AND review_state IN ('approved','modified')) WHERE batch_id=?",
                (batch_id, batch_id, batch_id),
            )
        for replay_id in replay_ids:
            sqlite.execute(
                "UPDATE cleaning_run SET stage='approved',approval_idempotency_key=?,last_error=NULL,updated_at=? WHERE replay_id=?",
                (request.idempotency_key, now, replay_id),
            )
        payload = {"targetCount": len(rows), "appliedCount": len(rows), "pendingCount": 0, "completedCount": len(rows), "status": "completed", "idempotencyKey": request.idempotency_key, "taskId": request.task_id, "sourceWrite": False, "formalPublication": False}
        sqlite.execute(
            "INSERT INTO audit_event(entity_type,entity_id,event_type,actor,payload_json,event_at) VALUES (?,?,?,?,?,?)",
            ("cleaning", "cleaning-batch", "cleaning_batch_approval_completed", actor, json.dumps(payload, ensure_ascii=False), now),
        )
        sqlite.commit()
        return payload
    except HTTPException:
        if sqlite.in_transaction:
            sqlite.rollback()
        raise
    except sqlite3.IntegrityError as exc:
        if sqlite.in_transaction:
            sqlite.rollback()
        raise HTTPException(status_code=409, detail=f"清洗批量审批写入冲突：{exc}") from exc
    finally:
        sqlite.close()


@domain_post("/api/cleaning/tasks/{task_id}/approve")
def approve_cleaning_task(task_id: str, request: CleaningTaskActionRequest, actor: str = Depends(require_decision_auth)) -> dict[str, Any]:
    """Approve one task; the old aggregate endpoint remains a compatibility wrapper."""
    return cleaning_batch_approve(
        CleaningBatchApprovalRequest(
            scope="cleaning",
            task_id=task_id,
            note=request.note,
            idempotency_key=request.idempotency_key,
        ),
        actor=actor,
    )


@domain_post("/api/cleaning/publish")
def cleaning_publish(request: CleaningBatchPublishRequest, actor: str = Depends(require_decision_auth)) -> dict[str, Any]:
    """Publish the already approved cleaning batch into the local formal layer."""
    sqlite = sqlite_connection()
    try:
        registry = cleaning_registry_map(sqlite)
        replay_ids = cleaning_task_replay_ids(sqlite, request.task_id)
        if not replay_ids:
            return {"targetCount": 0, "publishedCount": 0, "status": "empty", "idempotencyKey": request.idempotency_key, "sourceWrite": False}
        if request.task_id:
            task = cleaning_task_row(sqlite, request.task_id)
            if task["stage"] == "published":
                return {"targetCount": int(task["candidate_count"]), "publishedCount": int(task["published_count"]), "status": "already_completed", "idempotencyKey": request.idempotency_key, "taskId": task["cleaning_run_id"], "backupPath": task["backup_path"] or "", "sourceWrite": False, "formalPublication": True}
            if task["stage"] != "approved":
                raise HTTPException(status_code=409, detail="任务尚未完成审批，不能发布")
        marks = ",".join("?" for _ in replay_ids)
        prior = sqlite.execute(
            "SELECT payload_json FROM audit_event WHERE event_type='cleaning_batch_published' ORDER BY event_id DESC LIMIT 100"
        ).fetchall()
        for event in prior:
            try:
                payload = json.loads(event["payload_json"])
            except json.JSONDecodeError:
                continue
            if payload.get("idempotencyKey") == request.idempotency_key:
                return payload

        rows = sqlite.execute(
            f"""
            SELECT q.candidate_id,q.replay_id,c.batch_id,c.review_state,c.publication_state,
              c.validator_status,c.rule_version,c.validator_version,c.original_description,
              d.source_snapshot_id,d.source_schema,d.site_id,d.asset_number,
              r.review_id,r.reviewed_description,r.approval_receipt
            FROM formal_approval_queue q
            JOIN semantic_candidate c ON c.candidate_id=q.candidate_id
            JOIN device_identity d ON d.device_id=c.device_id
            JOIN review_decision r ON r.candidate_id=q.candidate_id
            WHERE q.replay_id IN ({marks}) AND q.status='modified'
            ORDER BY q.replay_id,q.candidate_id
            """,
            replay_ids,
        ).fetchall()
        if not rows:
            return {"targetCount": 0, "publishedCount": 0, "status": "already_completed", "idempotencyKey": request.idempotency_key, "sourceWrite": False}
        replay_rows = sqlite.execute(
            f"SELECT replay_id,status,evaluation_count,pass_count,fail_count FROM replay_run WHERE replay_id IN ({marks})",
            replay_ids,
        ).fetchall()
        if len(replay_rows) != len(replay_ids) or any(row["status"] != "passed" or int(row["fail_count"]) != 0 or int(row["evaluation_count"]) != int(row["pass_count"]) for row in replay_rows):
            raise HTTPException(status_code=409, detail="发布前校验失败：清洗规则回放未全部通过")
        invalid = [row["candidate_id"] for row in rows if row["review_state"] not in {"modified", "approved"} or row["publication_state"] != "unpublished" or row["validator_status"] != "candidate" or not str(row["reviewed_description"] or "").strip()]
        if invalid:
            raise HTTPException(status_code=409, detail=f"发布前校验失败：{len(invalid)} 条记录状态或描述不符合要求")

        duplicate = sqlite.execute(
            "SELECT candidate_id FROM published_description WHERE candidate_id IN ({})".format(",".join("?" for _ in rows)),
            [row["candidate_id"] for row in rows],
        ).fetchall()
        if duplicate:
            raise HTTPException(status_code=409, detail=f"发布前校验失败：已有 {len(duplicate)} 条正式结果，已停止本批次")

        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        backup_path = BACKUP_DIR / f"semantic_workflow_before_cleaning_publish_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.sqlite3"
        sqlite.execute("VACUUM INTO ?", (str(backup_path),))
        now = utc_now()
        run_id = f"cleaning-publication-{uuid.uuid4().hex}"
        sqlite.execute("BEGIN IMMEDIATE")
        for row in rows:
            final_description = row["reviewed_description"]
            publication_hash = hashlib.sha256(json.dumps({"candidateId": row["candidate_id"], "finalDescription": final_description, "replayId": row["replay_id"], "ruleVersion": row["rule_version"]}, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
            sqlite.execute(
                "INSERT INTO published_description(publication_id,candidate_id,review_id,source_snapshot_id,source_schema,site_id,asset_number,final_description,rule_version,validator_version,replay_id,published_by,published_at,publication_hash) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (f"publication-cleaning-{row['candidate_id']}", row["candidate_id"], row["review_id"], row["source_snapshot_id"], row["source_schema"], row["site_id"], row["asset_number"], final_description, row["rule_version"], row["validator_version"], row["replay_id"], actor, now, publication_hash),
            )
            sqlite.execute("UPDATE semantic_candidate SET publication_state='published',review_state='approved' WHERE candidate_id=?", (row["candidate_id"],))
            sqlite.execute("UPDATE formal_approval_queue SET updated_at=? WHERE candidate_id=?", (now, row["candidate_id"]))
        for batch_id in sorted({row["batch_id"] for row in rows}):
            sqlite.execute("UPDATE batch_run SET published_count=(SELECT count(*) FROM published_description p JOIN semantic_candidate c ON c.candidate_id=p.candidate_id WHERE c.batch_id=?) WHERE batch_id=?", (batch_id, batch_id))
        # Recalculate the task counters from the just-published local results
        # before recording the publication receipt.
        sync_cleaning_runs(sqlite, cleaning_registry_map(sqlite), now)
        for replay_id in replay_ids:
            sqlite.execute(
                """
                UPDATE cleaning_run
                SET stage='published',publication_run_id=?,backup_path=?,source_write=0,formal_publication=1,last_error=NULL,updated_at=?
                WHERE replay_id=?
                """,
                (run_id, str(backup_path), now, replay_id),
            )
        payload = {"publicationRunId": run_id, "targetCount": len(rows), "publishedCount": len(rows), "status": "published", "idempotencyKey": request.idempotency_key, "taskId": request.task_id, "backupPath": str(backup_path), "sourceWrite": False, "formalPublication": True}
        sqlite.execute("INSERT INTO audit_event(entity_type,entity_id,event_type,actor,payload_json,event_at) VALUES (?,?,?,?,?,?)", ("cleaning", run_id, "cleaning_batch_published", actor, json.dumps(payload, ensure_ascii=False), now))
        sqlite.commit()
        return payload
    except HTTPException:
        if sqlite.in_transaction:
            sqlite.rollback()
        raise
    except sqlite3.IntegrityError as exc:
        if sqlite.in_transaction:
            sqlite.rollback()
        raise HTTPException(status_code=409, detail=f"正式发布写入冲突：{exc}") from exc
    finally:
        sqlite.close()


@domain_post("/api/cleaning/tasks/{task_id}/publish")
def publish_cleaning_task(task_id: str, request: CleaningTaskActionRequest, actor: str = Depends(require_decision_auth)) -> dict[str, Any]:
    """Publish one approved task; publication remains idempotent and local-only."""
    return cleaning_publish(
        CleaningBatchPublishRequest(
            scope="cleaning",
            task_id=task_id,
            note=request.note,
            idempotency_key=request.idempotency_key,
        ),
        actor=actor,
    )


def published_filters(
    search: str | None,
    site_id: str | None,
    rule: str | None,
) -> tuple[str, list[Any]]:
    where: list[str] = []
    parameters: list[Any] = []
    if search and search.strip():
        # Keep search tolerant of source padding while preserving exact
        # asset-number and KKS lookup semantics.
        value = f"%{search.strip()}%"
        where.append(
            "(TRIM(p.asset_number) LIKE ? OR TRIM(p.final_description) LIKE ? "
            "OR TRIM(c.original_description) LIKE ? OR TRIM(d.location_code) LIKE ? "
            "OR TRIM(d.location_description) LIKE ?)"
        )
        parameters.extend([value] * 5)
    if site_id:
        where.append("p.site_id = ?")
        parameters.append(site_id)
    if rule:
        if rule == "format.fullwidth_parenthesis_to_ascii":
            where.append("p.final_description != c.original_description AND (c.original_description LIKE ? OR c.original_description LIKE ?)")
            parameters.extend(["%（%", "%）%"])
        elif rule == "format.fullwidth_comma_to_ascii":
            where.append("p.final_description != c.original_description AND c.original_description LIKE ?")
            parameters.append("%，%")
        else:
            where.append("c.applied_rule_ids_json LIKE ?")
            parameters.append(f"%{rule}%")
    return (" WHERE " + " AND ".join(where)) if where else "", parameters


@domain_get("/api/published")
def published(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    search: str | None = None,
    site_id: str | None = None,
    rule: str | None = None,
) -> dict[str, Any]:
    sqlite = sqlite_connection()
    try:
        where_sql, parameters = published_filters(search, site_id, rule)
        total = sqlite.execute(
            f"SELECT count(*) {PUBLISHED_SELECT[PUBLISHED_SELECT.index('FROM '):]}{where_sql}",
            parameters,
        ).fetchone()[0]
        rows = sqlite.execute(
            f"{PUBLISHED_SELECT}{where_sql} ORDER BY p.site_id,p.asset_number,p.publication_id LIMIT ? OFFSET ?",
            [*parameters, page_size, (page - 1) * page_size],
        ).fetchall()
        sites = sqlite.execute(
            "SELECT site_id,count(*) AS count FROM published_description GROUP BY site_id ORDER BY count DESC,site_id"
        ).fetchall()
        rules = sqlite.execute(
            """
            SELECT c.applied_rule_ids_json,count(*) AS row_count
            FROM semantic_candidate c
            JOIN published_description p ON p.candidate_id=c.candidate_id
            GROUP BY c.applied_rule_ids_json
            """
        ).fetchall()
        rule_counts: dict[str, int] = {}
        for row in rules:
            rule_keys = parse_json_array(row["applied_rule_ids_json"])
            for rule_key in set(rule_keys):
                rule_counts[rule_key] = rule_counts.get(rule_key, 0) + int(row["row_count"])
        return {
            "rows": [row_to_publication(row) for row in rows],
            "total": int(total),
            "page": page,
            "pageSize": page_size,
            "sites": [{"siteId": row["site_id"], "count": int(row["count"])} for row in sites],
            "rules": [{"rule": key, "count": count} for key, count in sorted(rule_counts.items())],
        }
    finally:
        sqlite.close()


@domain_get("/api/published/export")
def published_export_before_detail(
    search: str | None = None,
    site_id: str | None = None,
    rule: str | None = None,
) -> Response:
    """Keep the static export route ahead of the publication-id route."""
    return published_export(search=search, site_id=site_id, rule=rule)


@domain_get("/api/published/{publication_id}")
def published_detail(publication_id: str) -> dict[str, Any]:
    sqlite = sqlite_connection()
    try:
        row = sqlite.execute(
            f"{PUBLISHED_SELECT} WHERE p.publication_id=?",
            (publication_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="正式结果不存在")
        return row_to_publication(row)
    finally:
        sqlite.close()


def published_export(
    search: str | None = None,
    site_id: str | None = None,
    rule: str | None = None,
) -> Response:
    sqlite = sqlite_connection()
    try:
        where_sql, parameters = published_filters(search, site_id, rule)
        rows = sqlite.execute(
            f"{PUBLISHED_SELECT}{where_sql} ORDER BY p.site_id,p.asset_number,p.publication_id",
            parameters,
        ).fetchall()
    finally:
        sqlite.close()

    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(["SITEID", "ASSETNUM", "原始描述", "正式统一描述", "KKS", "位置", "分类", "规则版本", "发布批次", "发布时间"])
    writer.writerows([
        [row["site_id"], row["asset_number"], row["original_description"] or "", row["final_description"],
         row["location_code"] or "", row["location_description"] or "", row["classification_description"] or "",
         row["rule_version"], row["batch_id"], row["published_at"]]
        for row in rows
    ])
    return Response(
        content="\ufeff" + output.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="published_descriptions.csv"'},
    )


@domain_post("/api/reviews", response_model=ReviewResponse, response_model_by_alias=True)
def create_review(request: ReviewRequest, actor: str = Depends(require_decision_auth)) -> dict[str, Any]:
    description = (request.reviewed_description or "").strip()
    note = (request.note or "").strip()
    if request.decision in {"approved", "modified"} and not description:
        raise HTTPException(status_code=422, detail="通过或修改后通过必须填写最终统一描述")
    if request.decision in {"modified", "rejected", "deferred"} and not note:
        raise HTTPException(status_code=422, detail="该审核动作必须填写说明")

    sqlite = sqlite_connection()
    try:
        event = find_audit_event_by_idempotency(
            sqlite,
            event_type="review_submitted",
            idempotency_key=request.idempotency_key,
        )
        if event is not None:
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
        if candidate["review_state"] not in {"pending", "deferred"}:
            raise HTTPException(status_code=409, detail="该候选已经完成审核，不能重复提交")
        prior_review = sqlite.execute(
            "SELECT review_id FROM review_decision WHERE candidate_id=?",
            (request.candidate_id,),
        ).fetchone()
        if prior_review is not None:
            raise HTTPException(status_code=409, detail="该候选已经存在审核凭据，不能重复提交")
        if request.decision in {"approved", "modified"} and candidate["validator_status"] != "candidate":
            raise HTTPException(status_code=409, detail="只有通过硬校验的 candidate 才能批准或修改后通过")

        now = utc_now()
        review_id = f"review-{uuid.uuid4().hex}"
        approval_receipt = f"receipt-{uuid.uuid4().hex}"
        review_state = request.decision
        begin_write(sqlite)
        sqlite.execute(
            """
            INSERT INTO review_decision(review_id,candidate_id,decision,reviewed_description,reason_code,review_note,reviewer,approval_receipt,reviewed_at)
            VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (review_id, request.candidate_id, request.decision, description or None, f"REVIEW_{request.decision.upper()}", note or None, actor, approval_receipt, now),
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
        append_audit_event(
            sqlite,
            entity_type="review",
            entity_id=review_id,
            event_type="review_submitted",
            actor=actor,
            payload={"candidate_id": request.candidate_id, "decision": request.decision, "idempotency_key": request.idempotency_key},
            event_at=now,
        )
        sqlite.execute(
            "UPDATE formal_approval_queue SET status=?,note=?,updated_at=? WHERE candidate_id=?",
            (request.decision, note or "正式审批完成", now, request.candidate_id),
        )
        sqlite.execute(
            """
            UPDATE review_sample
            SET status=CASE WHEN NOT EXISTS (
              SELECT 1 FROM review_sample_item i
              JOIN semantic_candidate c ON c.candidate_id=i.candidate_id
              WHERE i.sample_id=review_sample.sample_id AND c.review_state='pending'
            ) THEN 'completed' ELSE status END
            WHERE sample_id IN (SELECT sample_id FROM review_sample_item WHERE candidate_id=?)
            """,
            (request.candidate_id,),
        )
        commit_write(sqlite)
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


# Stage 4: domain-owned route registration. Endpoint bodies remain compatible
# during the migration, while the app shell no longer owns these route paths.
from app.domains.registry import register_domain_routes
from app.domains.metadata.router import build_router as build_metadata_router
from app.domains.system.router import build_router as build_system_router

register_domain_routes(app, _DOMAIN_ROUTE_SPECS)
app.include_router(
    build_metadata_router(
        {
            "summary": metadata_summary,
            "catalog": metadata_catalog,
            "detail": metadata_catalog_detail,
            "export": metadata_export,
        }
    )
)
app.include_router(
    build_system_router(
        {
            "health": health,
            "metrics": semantic_metrics,
            "metrics_response_class": Response,
            "releases": semantic_releases,
            "release_detail": semantic_release_detail,
            "release_backup": semantic_release_backup,
            "release_approve": semantic_release_approve,
            "release_activate": semantic_release_activate,
            "release_rollback": semantic_release_rollback,
        }
    )
)
