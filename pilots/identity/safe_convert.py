"""Safe scalar coercions for dirty identity-pilot inputs.

Bare ``int(x)`` / ``float(x)`` raise on ``None``, empty strings and
non-numeric strings; these helpers clamp such inputs to a caller-provided
default instead of failing the whole run.
"""
from __future__ import annotations

from typing import Any


def to_int(value: Any, default: int = 0) -> int:
    """Coerce *value* to ``int``, returning *default* on dirty input."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def to_float(value: Any, default: float = 0.0) -> float:
    """Coerce *value* to ``float``, returning *default* on dirty input."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
