"""Published description routes (native APIRouter)."""
from __future__ import annotations

from fastapi import APIRouter


def build_router() -> APIRouter:
    from app.main import published, published_detail, published_export_before_detail

    router = APIRouter()
    router.add_api_route("/api/published", published, methods=["GET"])
    router.add_api_route("/api/published/export", published_export_before_detail, methods=["GET"])
    router.add_api_route("/api/published/{publication_id}", published_detail, methods=["GET"])
    return router
