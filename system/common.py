"""Shared helpers and canonical local database paths for system scripts.

This module is kept as a thin compatibility re-export of ``semantic_lib`` so
existing scripts can migrate without changing every import at once.
"""
from __future__ import annotations

from semantic_lib import (
    BACKUP_DIR,
    DATA_DIR,
    DEFAULT_DUCKDB,
    DEFAULT_METADATA,
    DEFAULT_SPEC,
    DEFAULT_TARGET,
    DEFAULT_WORKFLOW,
    ROOT,
    backup_before_publish,
    sha256_bytes,
    sha256_file,
    sid,
    utc_now,
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
    "utc_now",
    "sid",
    "sha256_file",
    "sha256_bytes",
    "backup_before_publish",
]
