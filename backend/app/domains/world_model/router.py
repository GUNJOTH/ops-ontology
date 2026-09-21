"""World-model HTTP route registration.

Business logic lives in focused service modules. This module deliberately
keeps only route paths and the compatibility export surface.
"""
from __future__ import annotations

from fastapi import APIRouter

from .context_service import world_model_device_context, world_model_device_context_payload, world_model_device_timeline
from .fact_explain_service import world_model_decision_explain, world_model_fact_explain
from .governance_service import (
    world_model_coverage,
    world_model_governance_contract,
    world_model_runtime_contract,
    world_model_summary,
)
from .identity_review_service import (
    decide_semantic_identity_review,
    revoke_semantic_identity,
    semantic_identity_review_detail,
    semantic_identity_review_isolate_non_device,
    semantic_identity_review_queue,
    semantic_identity_review_triage,
)


def build_router() -> APIRouter:
    router = APIRouter()
    router.add_api_route("/api/world-model/device/{unified_device_id}/context", world_model_device_context, methods=["GET"])
    router.add_api_route("/api/world-model/device/{unified_device_id}/timeline", world_model_device_timeline, methods=["GET"])
    router.add_api_route("/api/world-model/facts/{fact_id}/explain", world_model_fact_explain, methods=["GET"])
    router.add_api_route("/api/world-model/decisions/{decision_id}/explain", world_model_decision_explain, methods=["GET"])
    router.add_api_route("/api/world-model/summary", world_model_summary, methods=["GET"])
    router.add_api_route("/api/world-model/coverage", world_model_coverage, methods=["GET"])
    router.add_api_route("/api/world-model/runtime-contract", world_model_runtime_contract, methods=["GET"])
    router.add_api_route("/api/world-model/governance-contract", world_model_governance_contract, methods=["GET"])
    router.add_api_route("/api/world-model/identity/{assertion_id}/revoke", revoke_semantic_identity, methods=["POST"])
    router.add_api_route("/api/world-model/identity-review", semantic_identity_review_queue, methods=["GET"])
    router.add_api_route("/api/world-model/identity-review/triage", semantic_identity_review_triage, methods=["GET"])
    router.add_api_route("/api/world-model/identity-review/triage/isolate-non-device", semantic_identity_review_isolate_non_device, methods=["POST"])
    router.add_api_route("/api/world-model/identity-review/{review_id}", semantic_identity_review_detail, methods=["GET"])
    router.add_api_route("/api/world-model/identity-review/{review_id}", decide_semantic_identity_review, methods=["POST"])
    return router


__all__ = [
    "build_router",
    "world_model_device_context",
    "world_model_device_context_payload",
    "world_model_device_timeline",
    "world_model_fact_explain",
    "world_model_decision_explain",
    "world_model_summary",
    "world_model_coverage",
    "world_model_runtime_contract",
    "world_model_governance_contract",
    "revoke_semantic_identity",
    "semantic_identity_review_queue",
    "semantic_identity_review_triage",
    "semantic_identity_review_detail",
    "decide_semantic_identity_review",
]
