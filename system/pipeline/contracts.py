"""Shared pipeline contracts: safety, provenance, hashing and JSON output."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import sqlite3
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping


VOLATILE_KEYS = {
    "runid", "createdat", "updatedat", "finishedat", "startedat", "backuppath",
    "manifestpath", "durationseconds", "timestamp", "observedat",
}


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _stable_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _stable_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            if str(key).replace("_", "").lower() not in VOLATILE_KEYS
        }
    if isinstance(value, (list, tuple)):
        return [_stable_value(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def canonical_json(value: Any) -> str:
    return json.dumps(_stable_value(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def content_hash(*values: Any) -> str:
    digest = hashlib.sha256()
    for value in values:
        digest.update(canonical_json(value).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def idempotency_key(pipeline_id: str, pipeline_version: str, step_id: str, dependencies: Mapping[str, Any], parameters: Mapping[str, Any]) -> str:
    return f"{pipeline_id}:{pipeline_version}:{step_id}:{content_hash(dependencies, parameters)[:32]}"


def connect_readonly(path: Path, timeout: float = 30.0) -> sqlite3.Connection:
    """Open a SQLite source in read-only, query-only mode.

    All source readers should use this helper.  The URI mode prevents an
    accidental create/write when the path is missing.
    """
    resolved = Path(path).resolve()
    if not resolved.exists():
        raise FileNotFoundError(resolved)
    connection = sqlite3.connect(f"file:{resolved.as_posix()}?mode=ro", uri=True, timeout=timeout)
    connection.execute("PRAGMA query_only=ON")
    connection.row_factory = sqlite3.Row
    return connection


def connect_local(path: Path, timeout: float = 30.0) -> sqlite3.Connection:
    """Open a local semantic overlay database with common safe defaults."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path), timeout=timeout)
    connection.execute("PRAGMA foreign_keys=ON")
    connection.row_factory = sqlite3.Row
    return connection


def assert_safe_result(result: Mapping[str, Any]) -> None:
    """Enforce the pipeline-wide source-write/publication invariant."""
    unsafe = []
    for key in ("sourceWrite", "source_write", "formalPublication", "formal_publication"):
        if result.get(key) is True or result.get(key) == 1:
            unsafe.append(key)
    if unsafe:
        raise RuntimeError(f"pipeline safety boundary violated: {','.join(unsafe)}")


def write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    """Write a manifest atomically so interrupted runs cannot leave JSON half-written."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


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
