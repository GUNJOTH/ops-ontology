"""Route contract tests for canonical semantic query endpoints."""
from __future__ import annotations

import pytest
from app.main import app

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
        "/api/semantic/canonical/statements",
        "/api/semantic/canonical/device/{source_namespace}/{canonical_key}",
        "/api/semantic/sparql",
    }
    assert expected.issubset(set(paths))
    for path in expected:
        assert paths.count(path) == 1, f"duplicate semantic query route: {path}"
