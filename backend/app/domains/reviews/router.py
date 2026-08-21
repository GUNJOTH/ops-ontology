"""Review submission routes (native APIRouter)."""
from __future__ import annotations

from fastapi import APIRouter

from app.schemas.review import ReviewResponse


def build_router() -> APIRouter:
    from app.main import create_review

    router = APIRouter()
    router.add_api_route(
        "/api/reviews",
        create_review,
        methods=["POST"],
        response_model=ReviewResponse,
        response_model_by_alias=True,
    )
    return router
