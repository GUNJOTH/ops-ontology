"""Compatibility entrypoint for the enterprise operations semantic API.

Application assembly lives in :mod:`app.bootstrap`; legacy imports remain
available through :mod:`app.compat` during the domain migration.
"""
from __future__ import annotations

from app.bootstrap import app
from app.compat import *  # noqa: F401,F403
from app.compat import __all__ as _compat_exports

__all__ = ["app", *_compat_exports]
