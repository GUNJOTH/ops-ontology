"""Candidate AI-review compatibility facade.

The implementation is organized by responsibility:
- common: constants, file-backed sample and cache lifecycle
- scope: candidate selection and workload preview
- agent: model invocation and strict JSON normalization
- decisions: automatic approval and decision writes
- clusters: semantic-cluster browsing and whole-cluster decisions

The facade preserves the previous import surface for routers, tests and scripts.
"""
from __future__ import annotations

from .ai_agent import canonicalize_pending_candidate_reviews, invoke_pending_candidate_review
from .ai_clusters import ai_review_cluster_detail, ai_review_clusters, load_ai_cluster_rows, save_ai_cluster_decision
from .ai_decisions import (
    agent_audit_pending_candidates,
    ai_agent_review_preview,
    ai_auto_approve,
    ai_review_preview,
    ai_review_sample,
    ai_sample_summary,
    auto_approval_filter,
    auto_approval_preview,
    save_ai_bulk_decision,
    save_ai_review_decision,
)
from .ai_scope import candidate_agent_preview, candidate_agent_where
from .common import (
    AI_CLUSTER_CACHE_TTL_SECONDS,
    AUTO_APPROVAL_POLICY_VERSION,
    CANDIDATE_AGENT_DEFAULT_BATCH_SIZE,
    CANDIDATE_AGENT_EVIDENCE_LEVELS,
    CANDIDATE_AGENT_MAX_BATCH_SIZE,
    CANDIDATE_AGENT_REVIEW_VERSION,
    RULE_AGENT_EVIDENCE_SQL,
    invalidate_ai_cluster_cache,
    load_ai_review_sample,
)

__all__ = [
    "AI_CLUSTER_CACHE_TTL_SECONDS",
    "AUTO_APPROVAL_POLICY_VERSION",
    "CANDIDATE_AGENT_DEFAULT_BATCH_SIZE",
    "CANDIDATE_AGENT_EVIDENCE_LEVELS",
    "CANDIDATE_AGENT_MAX_BATCH_SIZE",
    "CANDIDATE_AGENT_REVIEW_VERSION",
    "RULE_AGENT_EVIDENCE_SQL",
    "candidate_agent_preview",
    "candidate_agent_where",
    "invoke_pending_candidate_review",
    "canonicalize_pending_candidate_reviews",
    "load_ai_review_sample",
    "invalidate_ai_cluster_cache",
    "load_ai_cluster_rows",
    "ai_sample_summary",
    "auto_approval_filter",
    "auto_approval_preview",
    "ai_review_preview",
    "ai_agent_review_preview",
    "ai_auto_approve",
    "agent_audit_pending_candidates",
    "ai_review_sample",
    "save_ai_review_decision",
    "save_ai_bulk_decision",
    "ai_review_clusters",
    "ai_review_cluster_detail",
    "save_ai_cluster_decision",
]
