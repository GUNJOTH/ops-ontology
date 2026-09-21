"""Compatibility facade for the rule-agent domain services.

The implementation is split by responsibility.  Existing routers, scripts
and tests may continue importing from this module while callers migrate to
the focused modules.
"""
from __future__ import annotations

from semantic_lib import sha256_file

from app.core.config import RULE_AGENT_DIR

from . import replay as _replay
from .agent_gateway import (
    invoke_rule_agent,
    invoke_rule_agent_review,
    invoke_semantic_reasoning_agent,
)
from .canonicalization import (
    canonicalize_semantic_reasoning,
    context_rule_spec_from_agent,
    semantic_reasoning_item_payload,
)
from .catalog import (
    build_rule_agent_local_catalog,
    build_semantic_context_clusters,
    rule_agent_profile,
    semantic_description_skeleton,
)
from .constants import (
    AGENT_REVIEW_VERSION,
    RULE_AGENT_CONTEXT_SAMPLE_LIMIT,
    RULE_AGENT_DISCOVERY_FILTER_VERSION,
    RULE_AGENT_EVIDENCE_SQL,
    RULE_AGENT_MIN_EVIDENCE_SAMPLES,
    RULE_AGENT_MIN_MATCH_COUNT,
)
from .proposals import (
    auto_process_accepted_rule_agent_proposal,
    canonicalize_rule_agent_proposals,
    deterministic_rule_review_gate,
    rule_agent_proposal_payload,
)
from .replay import (
    enrich_rule_agent_proposal_from_local_preview,
    evaluate_rule_agent_catalog_spec,
    measure_rule_agent_proposal,
    replay_rule_agent_against_evaluation_cases,
    rule_agent_case_scope_matches,
    rule_agent_proposal_row,
    rule_agent_target_rows,
)


def rule_agent_write_preview(proposal, rows):
    """Keep the historical monkeypatch/configuration surface for callers."""
    _replay.RULE_AGENT_DIR = RULE_AGENT_DIR
    return _replay.rule_agent_write_preview(proposal, rows)

__all__ = [
    "AGENT_REVIEW_VERSION",
    "RULE_AGENT_CONTEXT_SAMPLE_LIMIT",
    "RULE_AGENT_DISCOVERY_FILTER_VERSION",
    "RULE_AGENT_EVIDENCE_SQL",
    "RULE_AGENT_MIN_EVIDENCE_SAMPLES",
    "RULE_AGENT_MIN_MATCH_COUNT",
    "RULE_AGENT_DIR",
    "auto_process_accepted_rule_agent_proposal",
    "build_rule_agent_local_catalog",
    "build_semantic_context_clusters",
    "canonicalize_rule_agent_proposals",
    "canonicalize_semantic_reasoning",
    "context_rule_spec_from_agent",
    "deterministic_rule_review_gate",
    "enrich_rule_agent_proposal_from_local_preview",
    "evaluate_rule_agent_catalog_spec",
    "invoke_rule_agent",
    "invoke_rule_agent_review",
    "invoke_semantic_reasoning_agent",
    "measure_rule_agent_proposal",
    "replay_rule_agent_against_evaluation_cases",
    "rule_agent_case_scope_matches",
    "rule_agent_profile",
    "rule_agent_proposal_payload",
    "rule_agent_proposal_row",
    "rule_agent_target_rows",
    "rule_agent_write_preview",
    "sha256_file",
    "semantic_description_skeleton",
    "semantic_reasoning_item_payload",
]
