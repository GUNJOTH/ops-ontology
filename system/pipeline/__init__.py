"""Shared execution primitives for local semantic data pipelines.

This package is intentionally small and dependency-free.  It is the common
boundary for legacy system scripts while those scripts are migrated one by
one; it does not replace the existing semantic runtime tables.
"""

from .contracts import PipelineContext, connect_local, connect_readonly
from .dag import PipelineRunner, load_spec

__all__ = [
    "PipelineContext",
    "PipelineRunner",
    "connect_local",
    "connect_readonly",
    "load_spec",
]
