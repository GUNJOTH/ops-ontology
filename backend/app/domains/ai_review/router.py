"""AI review route registration."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter


def build_router(handlers: dict[str, Callable[..., Any]]) -> APIRouter:
    """Mount AI recommendation and review routes without changing behavior."""
    router = APIRouter()
    router.add_api_route("/api/ai-review/preview", handlers["preview"], methods=["GET"])
    router.add_api_route("/api/ai-review/agent-preview", handlers["agent_preview"], methods=["GET"])
    router.add_api_route("/api/ai-review/auto-approve", handlers["auto_approve"], methods=["POST"])
    router.add_api_route("/api/ai-review/agent-audit", handlers["agent_audit"], methods=["POST"])
    router.add_api_route("/api/ai-review/sample", handlers["sample"], methods=["GET"])
    router.add_api_route("/api/ai-review/decision", handlers["decision"], methods=["POST"])
    router.add_api_route("/api/ai-review/bulk-decision", handlers["bulk_decision"], methods=["POST"])
    router.add_api_route("/api/ai-review/clusters", handlers["clusters"], methods=["GET"])
    router.add_api_route("/api/ai-review/clusters/{cluster_id}", handlers["cluster_detail"], methods=["GET"])
    router.add_api_route("/api/ai-review/clusters/decision", handlers["cluster_decision"], methods=["POST"])
    return router
