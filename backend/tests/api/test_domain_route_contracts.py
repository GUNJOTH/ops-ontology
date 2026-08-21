"""Route contract tests for newly migrated domain routers.

These tests do not require a populated database.  They verify that every route
declared by a domain router is actually mounted on the FastAPI application and
exposed with the expected HTTP method in OpenAPI.
"""
from __future__ import annotations

import pytest
from app.domains.dashboard.router import build_router as build_dashboard_router
from app.domains.decisions.router import build_router as build_decisions_router
from app.domains.knowledge_assets.router import build_router as build_knowledge_assets_router
from app.domains.locations.router import build_router as build_locations_router
from app.domains.ontology.router import build_router as build_ontology_router
from app.domains.published.router import build_router as build_published_router
from app.domains.reviews.router import build_router as build_reviews_router
from app.domains.rule_agent.router import build_router as build_rule_agent_router
from app.domains.semantic_events.router import build_router as build_semantic_events_router
from app.domains.semantic_execution.router import build_router as build_semantic_execution_router
from app.domains.semantic_facts.router import build_router as build_semantic_facts_router
from app.domains.semantic_status.router import build_router as build_semantic_status_router
from app.domains.unified_devices.router import build_router as build_unified_devices_router
from app.domains.world_model.router import build_router as build_world_model_router
from app.main import app
from fastapi.testclient import TestClient

DOMAIN_ROUTERS = {
    "dashboard": build_dashboard_router,
    "decisions": build_decisions_router,
    "knowledge_assets": build_knowledge_assets_router,
    "locations": build_locations_router,
    "ontology": build_ontology_router,
    "published": build_published_router,
    "reviews": build_reviews_router,
    "rule_agent": build_rule_agent_router,
    "semantic_events": build_semantic_events_router,
    "semantic_execution": build_semantic_execution_router,
    "semantic_facts": build_semantic_facts_router,
    "semantic_status": build_semantic_status_router,
    "unified_devices": build_unified_devices_router,
    "world_model": build_world_model_router,
}

client = TestClient(app)
spec = client.get("/openapi.json").json()
paths = spec["paths"]


@pytest.mark.parametrize("domain_name", sorted(DOMAIN_ROUTERS))
def test_domain_routes_are_mounted(domain_name: str) -> None:
    router = DOMAIN_ROUTERS[domain_name]()
    assert router.routes, f"{domain_name} router should define at least one route"
    for route in router.routes:
        path = route.path
        methods = set(route.methods)
        assert path in paths, f"{domain_name}: missing path {path} in OpenAPI"
        available = {method.lower() for method in paths[path]}
        normalized = {method.lower() for method in methods}
        assert normalized <= available, f"{domain_name}: {path} expected {methods}, got {available}"
