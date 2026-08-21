"""System route registration kept separate from domain handlers."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter


def build_router(handlers: dict[str, Callable[..., Any]]) -> APIRouter:
    router = APIRouter()
    router.add_api_route("/api/health", handlers["health"], methods=["GET"])
    router.add_api_route(
        "/api/metrics",
        handlers["metrics"],
        methods=["GET"],
        response_class=handlers.get("metrics_response_class"),
    )
    router.add_api_route("/api/semantic/releases", handlers["releases"], methods=["GET"])
    router.add_api_route("/api/semantic/releases/{release_id}", handlers["release_detail"], methods=["GET"])
    router.add_api_route("/api/semantic/releases/{release_id}/backup", handlers["release_backup"], methods=["POST"])
    router.add_api_route("/api/semantic/releases/{release_id}/approve", handlers["release_approve"], methods=["POST"])
    router.add_api_route("/api/semantic/releases/{release_id}/activate", handlers["release_activate"], methods=["POST"])
    router.add_api_route("/api/semantic/releases/{release_id}/rollback", handlers["release_rollback"], methods=["POST"])
    return router
