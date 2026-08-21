"""FastAPI application assembly and startup lifecycle.

The application shell owns only startup, middleware and router mounting.  The
domain handlers remain in their domain modules and the local schema steps are
versioned under ``app.migrations``.
"""
from __future__ import annotations

import sys
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from app.core.config import DEPENDENCY_DIR

if DEPENDENCY_DIR.exists():
    sys.path.insert(0, str(DEPENDENCY_DIR))

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from app.core.db import configure_workflow_schema, raw_workflow_connection
from app.core.metrics import runtime_metrics
from app.domains.ai_review.router import build_router as build_ai_review_router
from app.domains.candidates.router import build_router as build_candidates_router
from app.domains.candidates.service import (
    agent_audit_pending_candidates,
    ai_agent_review_preview,
    ai_auto_approve,
    ai_review_cluster_detail,
    ai_review_clusters,
    ai_review_preview,
    ai_review_sample,
    candidate_detail,
    candidate_facets,
    candidates,
    formal_approval_queue,
    formal_batch_approve,
    save_ai_bulk_decision,
    save_ai_cluster_decision,
    save_ai_review_decision,
)
from app.domains.cleaning.router import build_router as build_cleaning_router
from app.domains.cleaning.service import (
    advance_cleaning_task,
    approve_cleaning_task,
    cleaning_batch_approve,
    cleaning_publish,
    cleaning_queue,
    cleaning_rules,
    cleaning_task,
    cleaning_tasks,
    gate_cleaning_replay,
    publish_cleaning_task,
    record_cleaning_preview,
    register_cleaning_rule,
)
from app.domains.dashboard.router import build_router as build_dashboard_router
from app.domains.decisions.router import build_router as build_decisions_router
from app.domains.knowledge_assets.router import build_router as build_knowledge_assets_router
from app.domains.locations.router import build_router as build_locations_router
from app.domains.metadata.router import build_router as build_metadata_router
from app.domains.ontology.router import build_router as build_ontology_router
from app.domains.published.router import build_router as build_published_router
from app.domains.reviews.router import build_router as build_reviews_router
from app.domains.rule_agent.router import build_router as build_rule_agent_router
from app.domains.semantic.router import build_router as build_semantic_router
from app.domains.semantic_events.router import build_router as build_semantic_events_router
from app.domains.semantic_execution.router import build_router as build_semantic_execution_router
from app.domains.semantic_facts.router import build_router as build_semantic_facts_router
from app.domains.semantic_status.router import build_router as build_semantic_status_router
from app.domains.system.router import build_router as build_system_router
from app.domains.unified_devices.router import build_router as build_unified_devices_router
from app.domains.world_model.router import build_router as build_world_model_router
from app.migrations.workflow_schema import migrate_workflow_schema
from app.migrations.workflow_steps import ensure_cleaning_schema, ensure_review_sample_schema


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Apply local workflow migrations once before serving requests."""
    connection = raw_workflow_connection()
    try:
        migrate_workflow_schema(
            connection,
            ("workflow-review-sample-v1", ensure_review_sample_schema),
            ("workflow-cleaning-v1", ensure_cleaning_schema),
        )
    finally:
        connection.close()
        configure_workflow_schema(None)
    yield


def build_app() -> FastAPI:
    application = FastAPI(title="设备语义治理 API", version="0.1.0", lifespan=lifespan)
    application.add_middleware(
        CORSMiddleware,
        allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )

    @application.middleware("http")
    async def observe_runtime_request(request: Request, call_next: Any):
        started = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        finally:
            route = request.scope.get("route")
            path = getattr(route, "path", request.url.path)
            runtime_metrics.inc(
                "semantic_http_requests_total",
                labels={"method": request.method, "path": path, "status": status_code},
            )
            runtime_metrics.observe(
                "semantic_http_request_duration_seconds",
                time.perf_counter() - started,
                labels={"method": request.method, "path": path},
            )

    application.include_router(build_metadata_router())
    application.include_router(
        build_candidates_router(
            {
                "list": candidates,
                "facets": candidate_facets,
                "detail": candidate_detail,
                "approval_queue": formal_approval_queue,
                "batch_approve": formal_batch_approve,
            }
        )
    )
    application.include_router(
        build_cleaning_router(
            {
                "tasks": cleaning_tasks,
                "task": cleaning_task,
                "preview": record_cleaning_preview,
                "replay": gate_cleaning_replay,
                "advance": advance_cleaning_task,
                "approve": approve_cleaning_task,
                "publish_task": publish_cleaning_task,
                "rules": cleaning_rules,
                "register_rule": register_cleaning_rule,
                "queue": cleaning_queue,
                "batch_approve": cleaning_batch_approve,
                "publish": cleaning_publish,
            }
        )
    )
    application.include_router(
        build_ai_review_router(
            {
                "preview": ai_review_preview,
                "agent_preview": ai_agent_review_preview,
                "auto_approve": ai_auto_approve,
                "agent_audit": agent_audit_pending_candidates,
                "sample": ai_review_sample,
                "decision": save_ai_review_decision,
                "bulk_decision": save_ai_bulk_decision,
                "clusters": ai_review_clusters,
                "cluster_detail": ai_review_cluster_detail,
                "cluster_decision": save_ai_cluster_decision,
            }
        )
    )
    for router_factory in (
        build_semantic_router,
        build_system_router,
        build_unified_devices_router,
        build_locations_router,
        build_knowledge_assets_router,
        build_semantic_facts_router,
        build_semantic_status_router,
        build_semantic_events_router,
        build_semantic_execution_router,
        build_decisions_router,
        build_ontology_router,
        build_world_model_router,
        build_rule_agent_router,
        build_dashboard_router,
        build_published_router,
        build_reviews_router,
    ):
        application.include_router(router_factory())
    return application


app = build_app()
