"""Canonical semantic query routes owned by the semantic domain.

Handlers are defined and registered in this module; ``service`` holds the
read-only payload builders.  ``app.main`` only mounts ``build_router()`` and
re-exports the handler names for import compatibility during the staged
migration.
"""
from __future__ import annotations

import json
import re
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.core.db import canonical_semantics_connection, identity_result_connection
from app.core.sparql_guard import guard_text
from app.schemas.semantic import CanonicalSparqlRequest

from .service import (
    canonical_device_payload,
    canonical_source_of_truth_payload,
    canonical_statements_payload,
    canonical_summary_payload,
    sparql_payload,
)

_READ_ONLY_SPARQL = re.compile(r"\b(SELECT|ASK)\b")
_BLOCKED_SPARQL = re.compile(r"\b(INSERT|DELETE|LOAD|CLEAR|DROP|CREATE|MOVE|COPY|ADD|SERVICE|UPDATE)\b")


def semantic_source_of_truth() -> dict[str, Any]:
    """Report the canonical-read cutover coverage without changing data."""
    canonical = canonical_semantics_connection()
    identity = identity_result_connection()
    try:
        return canonical_source_of_truth_payload(canonical, identity)
    finally:
        identity.close()
        canonical.close()


def canonical_semantic_summary() -> dict[str, Any]:
    """Return the canonical RDF Dataset run, graphs and safety state."""
    connection = canonical_semantics_connection()
    try:
        return canonical_summary_payload(connection)
    finally:
        connection.close()


def canonical_semantic_statements(
    subject_iri: str | None = Query(default=None, alias="subjectIri", max_length=1000),
    predicate_iri: str | None = Query(default=None, alias="predicateIri", max_length=1000),
    limit: int = Query(default=200, ge=1, le=1000),
) -> dict[str, Any]:
    """Read canonical RDF statements without exposing the source tables."""
    connection = canonical_semantics_connection()
    try:
        return canonical_statements_payload(connection, subject_iri, predicate_iri, limit)
    finally:
        connection.close()


def canonical_semantic_device(source_namespace: str, canonical_key: str) -> dict[str, Any]:
    """Return one device as JSON-LD-compatible data plus provenance."""
    connection = canonical_semantics_connection()
    try:
        try:
            return canonical_device_payload(connection, source_namespace, canonical_key)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=503, detail=f"Canonical JSON-LD context 不可用: {exc}") from exc
    finally:
        connection.close()


def canonical_semantic_sparql(request: CanonicalSparqlRequest) -> dict[str, Any]:
    """Execute a bounded read-only SPARQL 1.1 SELECT/ASK query on the latest graph."""
    query = request.query.strip()
    guard = guard_text(query).upper()
    if not _READ_ONLY_SPARQL.search(guard) or _BLOCKED_SPARQL.search(guard):
        raise HTTPException(
            status_code=422,
            detail="只允许只读 SPARQL SELECT/ASK；禁止更新、SERVICE 和外部访问",
        )
    connection = canonical_semantics_connection()
    try:
        try:
            return sparql_payload(connection, query)
        except ImportError as exc:
            raise HTTPException(status_code=503, detail="后端缺少 rdflib，请安装统一 Python 依赖：backend/requirements.lock") from exc
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=422, detail=f"SPARQL 查询无效：{exc}") from exc
    finally:
        connection.close()


def build_router() -> APIRouter:
    """Mount read-only semantic query endpoints and the guarded SPARQL API."""
    router = APIRouter()
    router.add_api_route("/api/semantic/source-of-truth", semantic_source_of_truth, methods=["GET"])
    router.add_api_route("/api/semantic/canonical/summary", canonical_semantic_summary, methods=["GET"])
    router.add_api_route("/api/semantic/canonical/statements", canonical_semantic_statements, methods=["GET"])
    router.add_api_route(
        "/api/semantic/canonical/device/{source_namespace}/{canonical_key}",
        canonical_semantic_device,
        methods=["GET"],
    )
    router.add_api_route("/api/semantic/sparql", canonical_semantic_sparql, methods=["POST"])
    return router
