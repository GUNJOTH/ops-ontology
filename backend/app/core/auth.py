"""Optional write guard and acting-reviewer resolution (F10).

The API is read-only by default and all state-changing writes are local-only.
When ``SEMANTIC_API_TOKEN`` is set, every state-changing write endpoint
additionally requires ``Authorization: Bearer <token>`` (401 otherwise).  When
it is unset, local no-token behaviour is unchanged.  The acting reviewer
recorded in those writes is resolved from the ``X-Reviewer`` header when
present, then the ``SEMANTIC_REVIEWER`` environment variable, then
``local-user``.

The token and reviewer are read from the environment on every call (not at
import time) so tests can flip ``SEMANTIC_API_TOKEN`` / ``SEMANTIC_REVIEWER``
with ``monkeypatch.setenv`` and have it take effect on the next request.
"""
from __future__ import annotations

import os

from fastapi import Header, HTTPException


def _token_from_env() -> str:
    return os.environ.get("SEMANTIC_API_TOKEN", "").strip()


def _reviewer_from_env() -> str:
    return os.environ.get("SEMANTIC_REVIEWER", "").strip()


def resolve_reviewer(x_reviewer: str | None) -> str:
    """Resolve the acting reviewer: X-Reviewer header > env > local-user."""
    if x_reviewer and x_reviewer.strip():
        return x_reviewer.strip()
    reviewer = _reviewer_from_env()
    if reviewer:
        return reviewer
    return "local-user"


def require_decision_auth(
    authorization: str | None = Header(default=None, alias="Authorization"),
    x_reviewer: str | None = Header(default=None, alias="X-Reviewer"),
) -> str:
    """Enforce the optional write token and return the acting reviewer.

    This dependency is the only authentication mechanism for the local write
    endpoints.  It intentionally does nothing when ``SEMANTIC_API_TOKEN`` is
    unset so existing local deployments keep working exactly as before.
    """
    token = _token_from_env()
    if token:
        expected = f"Bearer {token}"
        if authorization != expected:
            raise HTTPException(status_code=401, detail="缺少或无效的 API Token")
    return resolve_reviewer(x_reviewer)
