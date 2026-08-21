"""Cleaning workflow route registration."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter


def build_router(handlers: dict[str, Callable[..., Any]]) -> APIRouter:
    """Mount the cleaning task lifecycle without changing public endpoints."""
    router = APIRouter()
    router.add_api_route("/api/cleaning/tasks", handlers["tasks"], methods=["GET"])
    router.add_api_route("/api/cleaning/tasks/{task_id}", handlers["task"], methods=["GET"])
    router.add_api_route("/api/cleaning/tasks/{task_id}/preview", handlers["preview"], methods=["POST"])
    router.add_api_route("/api/cleaning/tasks/{task_id}/replay", handlers["replay"], methods=["POST"])
    router.add_api_route("/api/cleaning/tasks/{task_id}/advance", handlers["advance"], methods=["POST"])
    router.add_api_route("/api/cleaning/tasks/{task_id}/approve", handlers["approve"], methods=["POST"])
    router.add_api_route("/api/cleaning/tasks/{task_id}/publish", handlers["publish_task"], methods=["POST"])
    router.add_api_route("/api/cleaning/rules", handlers["rules"], methods=["GET"])
    router.add_api_route("/api/cleaning/rules", handlers["register_rule"], methods=["POST"])
    router.add_api_route("/api/cleaning", handlers["queue"], methods=["GET"])
    router.add_api_route("/api/cleaning/batch-approve", handlers["batch_approve"], methods=["POST"])
    router.add_api_route("/api/cleaning/publish", handlers["publish"], methods=["POST"])
    return router
