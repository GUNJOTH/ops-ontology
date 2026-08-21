"""Route contract tests for the AI-review domain extraction."""
from __future__ import annotations

import pytest
from app.main import app

pytestmark = pytest.mark.api


def test_ai_review_routes_are_mounted_once() -> None:
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
        "/api/ai-review/preview",
        "/api/ai-review/agent-preview",
        "/api/ai-review/auto-approve",
        "/api/ai-review/agent-audit",
        "/api/ai-review/sample",
        "/api/ai-review/decision",
        "/api/ai-review/bulk-decision",
        "/api/ai-review/clusters",
        "/api/ai-review/clusters/{cluster_id}",
        "/api/ai-review/clusters/decision",
    }
    assert expected.issubset(set(paths))
    for path in expected:
        assert paths.count(path) == 1, f"duplicate AI-review route: {path}"
