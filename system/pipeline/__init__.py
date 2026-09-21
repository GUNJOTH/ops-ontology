"""Shared execution primitives for local semantic data pipelines.

This package is intentionally small and dependency-free.  It is the common
boundary for legacy system scripts while those scripts are migrated one by
one; it does not replace the existing semantic runtime tables.
"""

from .contracts import PipelineContext, connect_local, connect_readonly
from .dag import PipelineRunner, load_spec
from .entrypoint import PipelineStepError, add_pipeline_arguments, load_resume_manifest, run_single_step

__all__ = [
    "PipelineContext",
    "PipelineRunner",
    "PipelineStepError",
    "add_pipeline_arguments",
    "connect_local",
    "connect_readonly",
    "load_resume_manifest",
    "load_spec",
    "run_single_step",
]
