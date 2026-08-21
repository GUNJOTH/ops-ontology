"""Canonical semantic query route registration."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter


def build_router(handlers: dict[str, Callable[..., Any]]) -> APIRouter:
    """Mount read-only semantic query endpoints and the guarded SPARQL API."""
    router = APIRouter()
    router.add_api_route("/api/semantic/source-of-truth", handlers["source_of_truth"], methods=["GET"])
    router.add_api_route("/api/semantic/canonical/summary", handlers["summary"], methods=["GET"])
    router.add_api_route("/api/semantic/canonical/statements", handlers["statements"], methods=["GET"])
    router.add_api_route(
        "/api/semantic/canonical/device/{source_namespace}/{canonical_key}",
        handlers["device"],
        methods=["GET"],
    )
    router.add_api_route("/api/semantic/sparql", handlers["sparql"], methods=["POST"])
    return router
