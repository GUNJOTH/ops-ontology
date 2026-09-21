"""Versioned local workflow schema steps used during application startup.

These migrations only touch the local workflow database.  Source systems and
formal semantic result stores are not opened by this module.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3

from app.core.utils import utc_now

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


def ensure_review_sample_schema(connection: sqlite3.Connection) -> None:
    """Create or extend the local AI review and approval tables."""
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
    for column, definition in {
        "error_code": "TEXT",
        "retryable": "INTEGER NOT NULL DEFAULT 0 CHECK (retryable IN (0,1))",
        "attempt_count": "INTEGER NOT NULL DEFAULT 0",
        "fallback_used": "INTEGER NOT NULL DEFAULT 0 CHECK (fallback_used IN (0,1))",
    }.items():
        if column not in run_columns:
            connection.execute(f"ALTER TABLE rule_agent_run ADD COLUMN {column} {definition}")
    for column, definition in {
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
    }.items():
        if column not in proposal_columns:
            connection.execute(f"ALTER TABLE rule_agent_proposal ADD COLUMN {column} {definition}")
    legacy_filtered = connection.execute(
        """
        SELECT count(*) FROM rule_agent_proposal
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
            SET discovery_filter_status='filtered', discovery_filter_reason='legacy_zero_local_evidence',
                discovery_filtered_at=?, updated_at=?
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
                    {"count": int(legacy_filtered), "reason": "legacy_zero_local_evidence", "sourceWrite": False, "formalPublication": False},
                    ensure_ascii=False,
                ),
                filtered_at,
            ),
        )
    connection.commit()
    ensure_cleaning_schema(connection)


def ensure_cleaning_schema(connection: sqlite3.Connection) -> None:
    """Create or extend the local cleaning task registry."""
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
    existing_columns = {row[1] for row in connection.execute("PRAGMA table_info(cleaning_run)").fetchall()}
    for column, definition in {
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
    }.items():
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
    """Compatibility hook for callers that still need both schema steps."""
    ensure_review_sample_schema(connection)
    ensure_cleaning_schema(connection)
