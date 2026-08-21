"""Shared rule-agent policy and version constants."""
from __future__ import annotations

RULE_AGENT_EVIDENCE_SQL = "c.evidence_level IN ('strong', 'source_preview_and_replay')"
RULE_AGENT_CONTEXT_SAMPLE_LIMIT = 10000
AGENT_REVIEW_VERSION = "rule-agent-review-20260814-v1"
RULE_AGENT_DISCOVERY_FILTER_VERSION = "rule-agent-local-evidence-20260814-v1"
RULE_AGENT_MIN_MATCH_COUNT = 1
RULE_AGENT_MIN_EVIDENCE_SAMPLES = 1
