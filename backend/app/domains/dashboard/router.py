"""Dashboard and review sample routes (native APIRouter)."""
from __future__ import annotations

from fastapi import APIRouter


def build_router() -> APIRouter:
    from app.main import dashboard, review_sample

    router = APIRouter()
    router.add_api_route("/api/dashboard", dashboard, methods=["GET"])
    router.add_api_route("/api/review-sample", review_sample, methods=["GET"])
    return router
