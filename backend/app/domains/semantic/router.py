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

from app.core.db import canonical_semantics_connection, identity_result_connection, unified_semantics_connection
from app.core.sparql_guard import guard_text
from app.schemas.semantic import AgentSemanticValidationRequest, CanonicalSparqlRequest, SemanticContextBuildRequest

from .agent_contract import validate_agent_semantic_result
from .service import (
    canonical_device_payload,
    canonical_object_payload,
    canonical_source_of_truth_payload,
    canonical_statements_payload,
    canonical_summary_payload,
    semantic_context_payload,
    semantic_object_timeline_payload,
    semantic_ontology_catalog_payload,
    semantic_package_payload,
    semantic_packages_payload,
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


def semantic_packages() -> dict[str, Any]:
    """Return the loaded platform-core and industry-package contract."""
    return semantic_packages_payload()


def semantic_package(package_id: str) -> dict[str, Any]:
    """Return one native package manifest and its declared assets."""
    try:
        return semantic_package_payload(package_id)
    except (LookupError, ValueError, OSError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def semantic_ontology_catalog() -> dict[str, Any]:
    """Return the composed OWL class/property catalog."""
    try:
        return semantic_ontology_catalog_payload()
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=503, detail=f"Canonical Ontology 不可用: {exc}") from exc


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


def semantic_object(object_type: str, canonical_key: str) -> dict[str, Any]:
    """Return one or more Canonical objects without cross-system merging."""
    connection = canonical_semantics_connection()
    try:
        try:
            return canonical_object_payload(connection, object_type, canonical_key)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
    finally:
        connection.close()


def _semantic_object_slice(object_type: str, canonical_key: str, field: str) -> dict[str, Any]:
    payload = semantic_object(object_type, canonical_key)
    return {
        "schemaVersion": "semantic-api-v1",
        "runId": payload["runId"],
        "objectType": payload["objectType"],
        "canonicalKey": payload["canonicalKey"],
        "matchCount": payload["matchCount"],
        "ambiguous": payload["ambiguous"],
        "items": [item[field] for item in payload["objects"]],
        "sourceWrite": False,
        "formalPublication": False,
    }


def semantic_object_relations(object_type: str, canonical_key: str) -> dict[str, Any]:
    return _semantic_object_slice(object_type, canonical_key, "relations")


def semantic_object_events(object_type: str, canonical_key: str) -> dict[str, Any]:
    return _semantic_object_slice(object_type, canonical_key, "events")


def semantic_object_facts(object_type: str, canonical_key: str) -> dict[str, Any]:
    return _semantic_object_slice(object_type, canonical_key, "facts")


def semantic_object_evidence(object_type: str, canonical_key: str) -> dict[str, Any]:
    return _semantic_object_slice(object_type, canonical_key, "evidence")


def semantic_object_timeline(object_type: str, canonical_key: str) -> dict[str, Any]:
    connection = canonical_semantics_connection()
    try:
        try:
            return semantic_object_timeline_payload(connection, object_type, canonical_key)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
    finally:
        connection.close()


def semantic_context_object(
    object_type: str,
    canonical_key: str,
    task: str = "",
    question: str = "",
    time_from: str | None = Query(default=None, alias="timeFrom", max_length=80),
    time_to: str | None = Query(default=None, alias="timeTo", max_length=80),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    canonical = canonical_semantics_connection()
    overlay = unified_semantics_connection()
    try:
        try:
            return semantic_context_payload(canonical, overlay, object_type, canonical_key, task, question, time_from, time_to, limit)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
    finally:
        overlay.close()
        canonical.close()


def semantic_context_build(request: SemanticContextBuildRequest) -> dict[str, Any]:
    return semantic_context_object(
        request.object_type,
        request.canonical_key,
        request.task,
        request.question,
        request.time_from,
        request.time_to,
        request.limit,
    )


def semantic_agent_validate(request: AgentSemanticValidationRequest) -> dict[str, Any]:
    return validate_agent_semantic_result(request.context, request.result)


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
    router.add_api_route("/api/semantic/packages", semantic_packages, methods=["GET"])
    router.add_api_route("/api/semantic/packages/{package_id}", semantic_package, methods=["GET"])
    router.add_api_route("/api/semantic/ontology/catalog", semantic_ontology_catalog, methods=["GET"])
    router.add_api_route("/api/semantic/canonical/statements", canonical_semantic_statements, methods=["GET"])
    router.add_api_route(
        "/api/semantic/canonical/device/{source_namespace}/{canonical_key}",
        canonical_semantic_device,
        methods=["GET"],
    )
    router.add_api_route("/api/semantic/object/{object_type}/{canonical_key}", semantic_object, methods=["GET"])
    router.add_api_route("/api/semantic/object/{object_type}/{canonical_key}/relations", semantic_object_relations, methods=["GET"])
    router.add_api_route("/api/semantic/object/{object_type}/{canonical_key}/events", semantic_object_events, methods=["GET"])
    router.add_api_route("/api/semantic/object/{object_type}/{canonical_key}/facts", semantic_object_facts, methods=["GET"])
    router.add_api_route("/api/semantic/object/{object_type}/{canonical_key}/evidence", semantic_object_evidence, methods=["GET"])
    router.add_api_route("/api/semantic/object/{object_type}/{canonical_key}/timeline", semantic_object_timeline, methods=["GET"])
    router.add_api_route("/api/semantic/context/object/{object_type}/{canonical_key}", semantic_context_object, methods=["GET"])
    router.add_api_route("/api/semantic/context/build", semantic_context_build, methods=["POST"])
    router.add_api_route("/api/semantic/agent/validate", semantic_agent_validate, methods=["POST"])
    router.add_api_route("/api/semantic/sparql", canonical_semantic_sparql, methods=["POST"])
    return router
