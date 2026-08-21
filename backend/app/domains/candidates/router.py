"""Candidate and formal-approval route registration."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter


def build_router(handlers: dict[str, Callable[..., Any]]) -> APIRouter:
    """Mount candidate workflow routes without changing their public paths."""
    router = APIRouter()
    router.add_api_route("/api/candidates", handlers["list"], methods=["GET"])
    router.add_api_route("/api/candidates/facets", handlers["facets"], methods=["GET"])
    router.add_api_route("/api/candidates/{candidate_id}", handlers["detail"], methods=["GET"])
    router.add_api_route("/api/formal-approval-queue", handlers["approval_queue"], methods=["GET"])
    router.add_api_route("/api/formal-approval-queue/batch-approve", handlers["batch_approve"], methods=["POST"])
    return router
