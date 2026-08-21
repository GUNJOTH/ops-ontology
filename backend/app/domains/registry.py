"""Ordered domain route registry.

Handlers remain importable during the staged migration, but route mounting is
centralized here so domain modules can be moved without changing API paths.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from fastapi import APIRouter, FastAPI


RouteSpec = tuple[str, list[str], Callable[..., Any], dict[str, Any]]


def register_domain_routes(app: FastAPI, specs: Iterable[RouteSpec]) -> None:
    router = APIRouter()
    for path, methods, endpoint, options in specs:
        router.add_api_route(path, endpoint, methods=methods, **options)
    app.include_router(router)

