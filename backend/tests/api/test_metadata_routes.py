"""Route contract tests for the native metadata-domain extraction."""
from __future__ import annotations

import pytest

from app.main import app

pytestmark = pytest.mark.api


def test_metadata_routes_are_mounted_once() -> None:
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
        "/api/metadata/summary",
        "/api/metadata/catalog",
        "/api/metadata/catalog/{semantic_id}",
        "/api/metadata/export",
    }
    assert expected.issubset(set(paths))
    for path in expected:
        assert paths.count(path) == 1, f"duplicate metadata route: {path}"
