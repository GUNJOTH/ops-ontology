"""Route contract tests for the candidate-domain extraction."""
from __future__ import annotations

import pytest

from app.main import app

pytestmark = pytest.mark.api


def test_candidate_routes_are_mounted_once() -> None:
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
        "/api/candidates",
        "/api/candidates/facets",
        "/api/candidates/{candidate_id}",
        "/api/formal-approval-queue",
        "/api/formal-approval-queue/batch-approve",
    }
    assert expected.issubset(set(paths))
    for path in expected:
        assert paths.count(path) == 1, f"duplicate candidate route: {path}"
