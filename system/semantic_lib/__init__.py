"""Shared paths, connections, hashing, safety and backup helpers.

This package is the single convergence point for system scripts and backend
runtime helpers.  Existing modules such as ``system.common`` and
``system.pipeline.contracts`` re-export from here for backward compatibility.
"""
from .core import (
    BACKUP_DIR,
    DATA_DIR,
    DEFAULT_DUCKDB,
    DEFAULT_METADATA,
    DEFAULT_SPEC,
    DEFAULT_TARGET,
    DEFAULT_WORKFLOW,
    ROOT,
    VOLATILE_KEYS,
    assert_safe_result,
    backup_before_publish,
    canonical_json,
    connect_local,
    connect_readonly,
    content_hash,
    idempotency_key,
    manifest_path,
    resolve_artifact_path,
    sha256_bytes,
    sha256_file,
    sid,
    utc_now,
    write_json_atomic,
)

__all__ = [
    "ROOT",
    "DATA_DIR",
    "DEFAULT_TARGET",
    "DEFAULT_WORKFLOW",
    "DEFAULT_DUCKDB",
    "DEFAULT_METADATA",
    "DEFAULT_SPEC",
    "BACKUP_DIR",
    "VOLATILE_KEYS",
    "utc_now",
    "sid",
    "sha256_file",
    "sha256_bytes",
    "connect_local",
    "connect_readonly",
    "assert_safe_result",
    "write_json_atomic",
    "canonical_json",
    "content_hash",
    "idempotency_key",
    "manifest_path",
    "resolve_artifact_path",
    "backup_before_publish",
]
