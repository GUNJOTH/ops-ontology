"""Route registration for the rule-agent domain.

Business flows live in dedicated modules; this file is intentionally limited
to the public HTTP contract and route composition.
"""
from __future__ import annotations

from fastapi import APIRouter

from .discovery import ai_review_rule_agent_proposals, discover_rules
from .lifecycle import (
    confirm_rule_agent_proposal,
    enable_rule_agent_proposal,
    preview_rule_agent_proposal,
    queue_rule_agent_proposal,
    replay_rule_agent_proposal,
)
from .queries import rule_agent_profile_endpoint, rule_agent_proposals, rule_agent_status
from .reasoning import semantic_reasoning_analyze, semantic_reasoning_latest


def build_router() -> APIRouter:
    router = APIRouter()
    router.add_api_route("/api/rule-agent/profile", rule_agent_profile_endpoint, methods=["GET"])
    router.add_api_route("/api/rule-agent/status", rule_agent_status, methods=["GET"])
    router.add_api_route("/api/semantic-reasoning/analyze", semantic_reasoning_analyze, methods=["POST"])
    router.add_api_route("/api/semantic-reasoning/latest", semantic_reasoning_latest, methods=["GET"])
    router.add_api_route("/api/rule-agent/proposals", rule_agent_proposals, methods=["GET"])
    router.add_api_route("/api/rule-agent/ai-review", ai_review_rule_agent_proposals, methods=["POST"])
    router.add_api_route("/api/rule-agent/discover", discover_rules, methods=["POST"])
    router.add_api_route("/api/rule-agent/proposals/{proposal_id}/preview", preview_rule_agent_proposal, methods=["POST"])
    router.add_api_route("/api/rule-agent/proposals/{proposal_id}/replay", replay_rule_agent_proposal, methods=["POST"])
    router.add_api_route("/api/rule-agent/proposals/{proposal_id}/confirm", confirm_rule_agent_proposal, methods=["POST"])
    router.add_api_route("/api/rule-agent/proposals/{proposal_id}/enable", enable_rule_agent_proposal, methods=["POST"])
    router.add_api_route("/api/rule-agent/proposals/{proposal_id}/queue", queue_rule_agent_proposal, methods=["POST"])
    return router
