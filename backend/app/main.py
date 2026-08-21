"""Read/query semantic workflow data and record auditable review decisions."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from app.core.config import (
    DEPENDENCY_DIR,
)

if DEPENDENCY_DIR.exists():
    sys.path.insert(0, str(DEPENDENCY_DIR))

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from app.core.db import (
    canonical_semantics_connection as _core_canonical_semantics_connection,
)
from app.core.db import (
    configure_workflow_schema,
)
from app.core.db import (
    duckdb_connection as _core_duckdb_connection,
)
from app.core.db import (
    identity_result_connection as _core_identity_result_connection,
)
from app.core.db import (
    latest_identity_result_db as _core_latest_identity_result_db,
)
from app.core.db import (
    metadata_sqlite_connection as _core_metadata_sqlite_connection,
)
from app.core.db import (
    raw_workflow_connection as _core_raw_workflow_connection,
)
from app.core.db import (
    sqlite_connection as _core_sqlite_connection,
)
from app.core.db import (
    unified_semantics_connection as _core_unified_semantics_connection,
)
from app.core.db import (
    unified_semantics_write_connection as _core_unified_semantics_write_connection,
)
from app.core.metrics import runtime_metrics
from app.core.utils import utc_now
from app.domains.candidates.service import (
    agent_audit_pending_candidates,
    ai_agent_review_preview,
    ai_auto_approve,
    ai_review_cluster_detail,
    ai_review_clusters,
    ai_review_preview,
    ai_review_sample,
    candidate_detail,
    candidate_facets,
    candidates,
    formal_approval_queue,
    formal_batch_approve,
    save_ai_bulk_decision,
    save_ai_cluster_decision,
    save_ai_review_decision,
)
from app.migrations.workflow_schema import migrate_workflow_schema


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Apply local workflow migrations once before serving requests.

    Schema work is startup-only: the initializer is passed explicitly to the
    lifespan handler and is never attached to request connections.
    """
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
    yield


app = FastAPI(title="设备语义治理 API", version="0.1.0", lifespan=lifespan)
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


# The optional write guard (SEMANTIC_API_TOKEN) and acting-reviewer resolution
# live in app.core.auth; the dependency is shared by all decision/review
# write endpoints across domains.

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


# Stage 3: schema work is startup-only and runs inside the application
# lifespan defined above; the initializer is never attached to request
# connections.
configure_workflow_schema(None)


def duckdb_connection() -> Any:
    return _core_duckdb_connection()


# Metadata handlers live in the metadata domain.  These aliases preserve the
# import surface used by existing tests and integrations while the app shell
# only mounts the domain router.

metadata_sqlite_connection = _core_metadata_sqlite_connection


# The current queue records the evidence produced by the preview/replay stage
# as ``source_preview_and_replay``. Older batches use ``strong``. Both are
# valid local evidence for rule discovery; neither permits source writes or
# formal publication by itself.




# Schema request/response models are domain-owned (app.schemas.*); these imports
# keep the app-shell import surface while the handlers live in the domain routers.
# Stage 4: domain-owned route registration. Endpoint bodies remain compatible
# during the migration, while the app shell no longer owns these route paths.
from app.domains.ai_review.router import build_router as build_ai_review_router
from app.domains.candidates.router import build_router as build_candidates_router
from app.domains.cleaning.router import build_router as build_cleaning_router
from app.domains.cleaning.service import (
    advance_cleaning_task,
    approve_cleaning_task,
    cleaning_batch_approve,
    cleaning_publish,
    cleaning_queue,
    cleaning_rules,
    cleaning_task,
    cleaning_tasks,
    gate_cleaning_replay,
    publish_cleaning_task,
    record_cleaning_preview,
    register_cleaning_rule,
)
from app.domains.dashboard.router import build_router as build_dashboard_router
from app.domains.decisions.router import build_router as build_decisions_router
from app.domains.knowledge_assets.router import build_router as build_knowledge_assets_router
from app.domains.locations.router import build_router as build_locations_router
from app.domains.metadata.router import build_router as build_metadata_router
from app.domains.ontology.router import build_router as build_ontology_router
from app.domains.published.router import build_router as build_published_router
from app.domains.reviews.router import build_router as build_reviews_router
from app.domains.rule_agent.router import build_router as build_rule_agent_router
from app.domains.semantic.router import build_router as build_semantic_router
from app.domains.semantic_events.router import build_router as build_semantic_events_router
from app.domains.semantic_execution.router import build_router as build_semantic_execution_router
from app.domains.semantic_facts.router import build_router as build_semantic_facts_router
from app.domains.semantic_status.router import build_router as build_semantic_status_router
from app.domains.system.router import build_router as build_system_router
from app.domains.unified_devices.router import build_router as build_unified_devices_router
from app.domains.world_model.router import build_router as build_world_model_router
from app.schemas.semantic import (
    CanonicalSparqlRequest,
)

app.include_router(build_metadata_router())
app.include_router(
    build_candidates_router(
        {
            "list": candidates,
            "facets": candidate_facets,
            "detail": candidate_detail,
            "approval_queue": formal_approval_queue,
            "batch_approve": formal_batch_approve,
        }
    )
)
app.include_router(
    build_cleaning_router(
        {
            "tasks": cleaning_tasks,
            "task": cleaning_task,
            "preview": record_cleaning_preview,
            "replay": gate_cleaning_replay,
            "advance": advance_cleaning_task,
            "approve": approve_cleaning_task,
            "publish_task": publish_cleaning_task,
            "rules": cleaning_rules,
            "register_rule": register_cleaning_rule,
            "queue": cleaning_queue,
            "batch_approve": cleaning_batch_approve,
            "publish": cleaning_publish,
        }
    )
)
app.include_router(
    build_ai_review_router(
        {
            "preview": ai_review_preview,
            "agent_preview": ai_agent_review_preview,
            "auto_approve": ai_auto_approve,
            "agent_audit": agent_audit_pending_candidates,
            "sample": ai_review_sample,
            "decision": save_ai_review_decision,
            "bulk_decision": save_ai_bulk_decision,
            "clusters": ai_review_clusters,
            "cluster_detail": ai_review_cluster_detail,
            "cluster_decision": save_ai_cluster_decision,
        }
    )
)
app.include_router(build_semantic_router())
app.include_router(build_system_router())
app.include_router(build_unified_devices_router())
app.include_router(build_locations_router())
app.include_router(build_knowledge_assets_router())
app.include_router(build_semantic_facts_router())
app.include_router(build_semantic_status_router())
app.include_router(build_semantic_events_router())
app.include_router(build_semantic_execution_router())
app.include_router(build_decisions_router())
app.include_router(build_ontology_router())
app.include_router(build_world_model_router())
app.include_router(build_rule_agent_router())
app.include_router(build_dashboard_router())
app.include_router(build_published_router())
app.include_router(build_reviews_router())


# Compatibility re-export surface (Phase 1 bridge): 迁移期间既有测试与集成继续
# 从 app.main 导入这些名字。显式 __all__ 声明导出面，静态检查据此不再把这些
# 纯再导出当作未使用 import 处理。随域迁移收尾（S2/S3）退役整段。
# Compatibility re-exports for migrated domain implementations.
from app.domains.decisions.router import (
    decision_layer_tables,
    review_semantic_action_plan,
    semantic_action_catalog,
    semantic_action_plan_detail,
    semantic_action_plans,
    semantic_action_plans_summary,
    semantic_decisions,
    semantic_decisions_summary,
)
from app.domains.knowledge_assets.router import (
    knowledge_asset_detail,
    knowledge_asset_summary,
    knowledge_assets,
)
from app.domains.knowledge_assets.service import knowledge_asset_row
from app.domains.locations.router import (
    unified_location_detail,
    unified_location_summary,
    unified_locations,
)
from app.domains.locations.service import unified_location_row
from app.domains.metadata.router import (
    metadata_catalog,
    metadata_catalog_detail,
    metadata_export,
    metadata_summary,
)
from app.domains.ontology.router import (
    ontology_event_types,
    ontology_meta_summary,
    ontology_object_types,
    ontology_relation_types,
)
from app.domains.ontology.service import ontology_meta_tables
from app.domains.semantic.router import (
    canonical_semantic_device,
    canonical_semantic_sparql,
    canonical_semantic_statements,
    canonical_semantic_summary,
    semantic_source_of_truth,
)
from app.domains.semantic_events.router import (
    semantic_event_detail,
    semantic_events,
    semantic_events_summary,
)
from app.domains.semantic_execution.router import (
    semantic_execution_dispatch,
    semantic_execution_preview,
    semantic_execution_summary,
)
from app.domains.semantic_facts.router import (
    semantic_fact_detail,
    semantic_facts,
    semantic_facts_summary,
)
from app.domains.semantic_status.router import (
    execute_status_mapping_replay_local,
    replay_semantic_states,
    replay_semantic_status_dictionary,
    review_semantic_status_dictionary,
    semantic_state_replay_diffs,
    semantic_states_summary,
    semantic_status_dictionary,
    semantic_status_dictionary_detail,
    semantic_status_dictionary_summary,
)
from app.domains.system.router import (
    health,
    semantic_metrics,
    semantic_release_activate,
    semantic_release_approve,
    semantic_release_backup,
    semantic_release_detail,
    semantic_release_rollback,
    semantic_releases,
)
from app.domains.unified_devices.router import (
    unified_device_detail,
    unified_device_summary,
    unified_devices,
)
from app.domains.unified_devices.service import unified_device_list_row
from app.domains.world_model.router import (
    decide_semantic_identity_review,
    revoke_semantic_identity,
    semantic_identity_review_detail,
    semantic_identity_review_queue,
    world_model_coverage,
    world_model_decision_explain,
    world_model_device_context,
    world_model_device_context_payload,
    world_model_device_timeline,
    world_model_fact_explain,
    world_model_governance_contract,
    world_model_runtime_contract,
    world_model_summary,
)

__all__ = [
    "CanonicalSparqlRequest",
    "metadata_catalog",
    "metadata_catalog_detail",
    "metadata_export",
    "metadata_summary",
    "semantic_source_of_truth",
    "canonical_semantic_summary",
    "canonical_semantic_statements",
    "canonical_semantic_device",
    "canonical_semantic_sparql",
    "health",
    "semantic_metrics",
    "semantic_releases",
    "semantic_release_detail",
    "semantic_release_backup",
    "semantic_release_approve",
    "semantic_release_activate",
    "semantic_release_rollback",

    "decision_layer_tables",
    "knowledge_asset_detail",
    "knowledge_asset_row",
    "knowledge_assets",
    "knowledge_asset_summary",
    "ontology_event_types",
    "ontology_meta_summary",
    "ontology_meta_tables",
    "ontology_object_types",
    "ontology_relation_types",
    "replay_semantic_status_dictionary",
    "replay_semantic_states",
    "review_semantic_action_plan",
    "review_semantic_status_dictionary",
    "semantic_action_catalog",
    "semantic_action_plan_detail",
    "semantic_action_plans",
    "semantic_action_plans_summary",
    "semantic_decisions",
    "semantic_decisions_summary",
    "semantic_event_detail",
    "semantic_events",
    "semantic_events_summary",
    "semantic_execution_dispatch",
    "semantic_execution_preview",
    "semantic_execution_summary",
    "semantic_fact_detail",
    "semantic_facts",
    "semantic_facts_summary",
    "semantic_identity_review_detail",
    "semantic_identity_review_queue",
    "semantic_state_replay_diffs",
    "semantic_status_dictionary",
    "semantic_status_dictionary_detail",
    "semantic_status_dictionary_summary",
    "semantic_states_summary",
    "unified_device_detail",
    "unified_device_list_row",
    "unified_devices",
    "unified_device_summary",
    "unified_location_detail",
    "unified_location_row",
    "unified_locations",
    "unified_location_summary",
    "world_model_coverage",
    "world_model_decision_explain",
    "world_model_device_context",
    "world_model_device_context_payload",
    "world_model_device_timeline",
    "world_model_fact_explain",
    "world_model_governance_contract",
    "world_model_runtime_contract",
    "world_model_summary",
    "decide_semantic_identity_review",
    "revoke_semantic_identity",
    "execute_status_mapping_replay_local",
]
