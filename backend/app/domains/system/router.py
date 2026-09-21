"""System routes owned by the system domain (health, metrics, releases).

Handlers are defined and registered in this module; ``app.main`` only mounts
``build_router()`` and re-exports the handler names for import compatibility
during the staged migration.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response

from app.core.auth import require_decision_auth
from app.core.config import CANONICAL_SEMANTICS_DB, DUCKDB_DB, SQLITE_DB
from app.core.metrics import runtime_metrics
from app.core.semantic_release import (
    _load_release,
    activate_release,
    approve_release,
    backup_release,
    load_active_release,
    load_registry,
    rollback_release,
)
from app.schemas.system import SemanticReleaseApprovalRequest


def health() -> dict[str, Any]:
    sqlite_ok = SQLITE_DB.exists()
    duckdb_ok = DUCKDB_DB.exists()
    canonical_ok = CANONICAL_SEMANTICS_DB.exists()
    active_release = load_active_release()
    return {
        "status": "ok" if sqlite_ok and duckdb_ok and canonical_ok else "degraded",
        "sqlite": sqlite_ok,
        "duckdb": duckdb_ok,
        "canonicalRdf": canonical_ok,
        "activeRelease": active_release,
        "metricSeries": len(runtime_metrics.snapshot()["counters"]) + len(runtime_metrics.snapshot()["observations"]),
        "sourceWrite": False,
        "formalPublication": False,
    }


def semantic_releases() -> dict[str, Any]:
    """Return the local Semantic Release registry and active pointer."""
    registry = load_registry()
    return {
        "schemaVersion": "semantic-release-v1",
        "active": load_active_release(),
        "releases": registry.get("releases", []),
        "sourceWrite": False,
        "formalPublication": False,
    }


def semantic_release_detail(release_id: str) -> dict[str, Any]:
    try:
        return _load_release(release_id)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def semantic_release_backup(release_id: str, actor: str = Depends(require_decision_auth)) -> dict[str, Any]:
    """Create and checksum the immutable local RDF Dataset backup."""
    try:
        result = backup_release(release_id)
        result["actor"] = actor
        return result
    except (FileNotFoundError, FileExistsError, RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def semantic_release_approve(
    release_id: str,
    request: SemanticReleaseApprovalRequest,
    actor: str = Depends(require_decision_auth),
) -> dict[str, Any]:
    """Record production approval locally; it does not publish source data."""
    reviewer = request.reviewer.strip() or actor
    try:
        result = approve_release(release_id, reviewer, request.receipt, request.note)
        result["sourceWrite"] = False
        result["formalPublication"] = False
        return result
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def semantic_release_activate(release_id: str, actor: str = Depends(require_decision_auth)) -> dict[str, Any]:
    """Activate a previously approved and backed-up local Canonical release."""
    try:
        return activate_release(release_id, actor)
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def semantic_release_rollback(release_id: str, actor: str = Depends(require_decision_auth)) -> dict[str, Any]:
    """Move the local read pointer to a previously approved release."""
    try:
        return rollback_release(release_id, actor)
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def semantic_metrics() -> Response:
    """Prometheus-compatible local process metrics."""
    return Response(content=runtime_metrics.prometheus(), media_type="text/plain; version=0.0.4")


def build_router() -> APIRouter:
    router = APIRouter()
    router.add_api_route("/api/health", health, methods=["GET"])
    router.add_api_route(
        "/api/metrics",
        semantic_metrics,
        methods=["GET"],
        response_class=Response,
    )
    router.add_api_route("/api/semantic/releases", semantic_releases, methods=["GET"])
    router.add_api_route("/api/semantic/releases/{release_id}", semantic_release_detail, methods=["GET"])
    router.add_api_route("/api/semantic/releases/{release_id}/backup", semantic_release_backup, methods=["POST"])
    router.add_api_route("/api/semantic/releases/{release_id}/approve", semantic_release_approve, methods=["POST"])
    router.add_api_route("/api/semantic/releases/{release_id}/activate", semantic_release_activate, methods=["POST"])
    router.add_api_route("/api/semantic/releases/{release_id}/rollback", semantic_release_rollback, methods=["POST"])
    return router
