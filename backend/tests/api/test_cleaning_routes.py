"""Route contract tests for the cleaning-domain extraction."""
from __future__ import annotations

import pytest

from app.main import app

pytestmark = pytest.mark.api


def test_cleaning_routes_are_mounted_once() -> None:
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
        "/api/cleaning/tasks",
        "/api/cleaning/tasks/{task_id}",
        "/api/cleaning/tasks/{task_id}/preview",
        "/api/cleaning/tasks/{task_id}/replay",
        "/api/cleaning/tasks/{task_id}/advance",
        "/api/cleaning/tasks/{task_id}/approve",
        "/api/cleaning/tasks/{task_id}/publish",
        "/api/cleaning",
        "/api/cleaning/batch-approve",
        "/api/cleaning/publish",
    }
    assert expected.issubset(set(paths))
    for path in expected:
        assert paths.count(path) == 1, f"duplicate cleaning route: {path}"
    assert paths.count("/api/cleaning/rules") == 2
