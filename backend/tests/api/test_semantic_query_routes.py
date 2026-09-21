"""Route contract tests for canonical semantic query endpoints."""
from __future__ import annotations

import pytest
from app.main import app
from fastapi.testclient import TestClient

pytestmark = pytest.mark.api


def test_semantic_query_routes_are_mounted_once() -> None:
    def collect(routes):
        values = []
        for route in routes:
            nested = getattr(route, "original_router", None)
            if nested is not None:
                values.extend(collect(nested.routes))
            else:
                values.append(getattr(route, "path", ""))
        return values

    paths = collect(app.router.routes)
    expected = {
        "/api/semantic/source-of-truth",
        "/api/semantic/canonical/summary",
        "/api/semantic/packages",
        "/api/semantic/packages/{package_id}",
        "/api/semantic/ontology/catalog",
        "/api/semantic/canonical/statements",
        "/api/semantic/canonical/device/{source_namespace}/{canonical_key}",
        "/api/semantic/sparql",
        "/api/semantic/object/{object_type}/{canonical_key}/timeline",
        "/api/semantic/context/object/{object_type}/{canonical_key}",
        "/api/semantic/context/build",
    }
    assert expected.issubset(set(paths))
    for path in expected:
        assert paths.count(path) == 1, f"duplicate semantic query route: {path}"


def test_semantic_v2_package_and_ontology_catalog_are_read_only() -> None:
    client = TestClient(app)
    package_response = client.get("/api/semantic/packages/platform-core")
    assert package_response.status_code == 200
    package = package_response.json()
    assert package["id"] == "platform-core"
    assert package["sourceWrite"] is False
    assert package["formalPublication"] is False

    catalog_response = client.get("/api/semantic/ontology/catalog")
    assert catalog_response.status_code == 200
    catalog = catalog_response.json()
    assert catalog["ontologyVersion"] == "enterprise-operations-ontology/v2"
    assert any(item["name"] == "Device" for item in catalog["classes"])
    assert catalog["sourceWrite"] is False
    assert catalog["formalPublication"] is False


def test_knowledge_v3_routes_are_read_only_aliases() -> None:
    def collect(routes):
        values = []
        for route in routes:
            nested = getattr(route, "original_router", None)
            if nested is not None:
                values.extend(collect(nested.routes))
            else:
                values.append(getattr(route, "path", ""))
        return values

    paths = set(collect(app.router.routes))
    assert "/api/semantic/knowledge" in paths
    assert "/api/semantic/knowledge/{asset_id}/evidence" in paths
    assert "/api/semantic/object/{object_type}/{object_key}/knowledge" in paths
