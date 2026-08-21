"""Shared pipeline contracts: safety, provenance, hashing and JSON output.

The low-level shared helpers are implemented once in ``system.semantic_lib``;
this module keeps the pipeline-specific ``PipelineContext`` and re-exports the
common helpers for backward compatibility.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from semantic_lib import (
    VOLATILE_KEYS,
    assert_safe_result,
    canonical_json,
    connect_local,
    connect_readonly,
    content_hash,
    idempotency_key,
    manifest_path,
    resolve_artifact_path,
    utc_now,
    write_json_atomic,
)

__all__ = [
    "VOLATILE_KEYS",
    "utc_now",
    "canonical_json",
    "content_hash",
    "idempotency_key",
    "manifest_path",
    "resolve_artifact_path",
    "connect_readonly",
    "connect_local",
    "assert_safe_result",
    "write_json_atomic",
    "PipelineContext",
]


@dataclass(frozen=True)
class PipelineContext:
    pipeline_id: str
    pipeline_version: str
    run_id: str
    root: Path
    parameters: Mapping[str, Any] = field(default_factory=dict)
    manifest_path: Path | None = None
    resume_manifest: Mapping[str, Any] | None = None

    def with_manifest(self, payload: Mapping[str, Any]) -> None:
        if self.manifest_path is not None:
            write_json_atomic(self.manifest_path, payload)
