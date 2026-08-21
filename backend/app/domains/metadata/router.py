"""Metadata route registration kept separate from the application shell."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter


def build_router(handlers: dict[str, Callable[..., Any]]) -> APIRouter:
    router = APIRouter()
    router.add_api_route("/api/metadata/summary", handlers["summary"], methods=["GET"])
    router.add_api_route("/api/metadata/catalog", handlers["catalog"], methods=["GET"])
    router.add_api_route("/api/metadata/catalog/{semantic_id}", handlers["detail"], methods=["GET"])
    router.add_api_route("/api/metadata/export", handlers["export"], methods=["GET"])
    return router

