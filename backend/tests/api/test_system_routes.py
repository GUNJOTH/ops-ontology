"""Route contract tests for the first native system-domain extraction."""
from __future__ import annotations

import pytest
from app.main import app

pytestmark = pytest.mark.api


def test_system_routes_are_mounted_once() -> None:
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
        "/api/health",
        "/api/metrics",
        "/api/semantic/releases",
        "/api/semantic/releases/{release_id}",
        "/api/semantic/releases/{release_id}/backup",
        "/api/semantic/releases/{release_id}/approve",
        "/api/semantic/releases/{release_id}/activate",
        "/api/semantic/releases/{release_id}/rollback",
    }
    assert expected.issubset(set(paths))
    for path in expected:
        assert paths.count(path) == 1, f"duplicate system route: {path}"
