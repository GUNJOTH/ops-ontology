"""Candidate/review shared service facade.

The implementation has been split into focused submodules:

- ``cleaning_sync``: cleaning registry state alignment
- ``samples``: latest batch and review-sample helpers
- ``ai_review``: AI review, clustering and agent previews
- ``candidate_queries``: candidate list/facet/detail queries
- ``approvals``: formal approval queue and batch approval

This module preserves the previous import surface so existing routers, tests
and scripts can continue to import from ``app.domains.candidates.service``.
"""
from __future__ import annotations

from .ai_review import (
    AI_CLUSTER_CACHE_TTL_SECONDS,
    CANDIDATE_AGENT_DEFAULT_BATCH_SIZE,
    CANDIDATE_AGENT_EVIDENCE_LEVELS,
    CANDIDATE_AGENT_MAX_BATCH_SIZE,
    CANDIDATE_AGENT_REVIEW_VERSION,
    RULE_AGENT_EVIDENCE_SQL,
    agent_audit_pending_candidates,
    ai_agent_review_preview,
    ai_auto_approve,
    ai_review_cluster_detail,
    ai_review_clusters,
    ai_review_preview,
    ai_review_sample,
    ai_sample_summary,
    auto_approval_filter,
    auto_approval_preview,
    candidate_agent_preview,
    candidate_agent_where,
    canonicalize_pending_candidate_reviews,
    invalidate_ai_cluster_cache,
    invoke_pending_candidate_review,
    load_ai_cluster_rows,
    load_ai_review_sample,
    save_ai_bulk_decision,
    save_ai_cluster_decision,
    save_ai_review_decision,
)
from .approvals import formal_approval_queue, formal_batch_approve
from .candidate_queries import candidate_detail, candidate_facets, candidates
from .cleaning_sync import cleaning_registry, cleaning_registry_map, sync_cleaning_runs
from .samples import (
    DEFAULT_SAMPLE_TARGET,
    MAX_SAMPLE_TARGET,
    ensure_review_sample,
    latest_batch,
    review_sample_summary,
)

__all__ = [
    "DEFAULT_SAMPLE_TARGET",
    "MAX_SAMPLE_TARGET",
    "CANDIDATE_AGENT_MAX_BATCH_SIZE",
    "CANDIDATE_AGENT_EVIDENCE_LEVELS",
    "RULE_AGENT_EVIDENCE_SQL",
    "AI_CLUSTER_CACHE_TTL_SECONDS",
    "CANDIDATE_AGENT_REVIEW_VERSION",
    "CANDIDATE_AGENT_DEFAULT_BATCH_SIZE",
    "cleaning_registry",
    "cleaning_registry_map",
    "sync_cleaning_runs",
    "latest_batch",
    "ensure_review_sample",
    "review_sample_summary",
    "candidate_agent_where",
    "candidate_agent_preview",
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
    "candidates",
    "candidate_facets",
    "candidate_detail",
    "formal_approval_queue",
    "formal_batch_approve",
]
