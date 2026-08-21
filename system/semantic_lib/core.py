"""Shared ops-ontology infrastructure.

Kept dependency-free (stdlib only) so both backend and system scripts can use
it without creating a package dependency cycle.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import pathlib
import shutil
import sqlite3
import tempfile
from typing import Any, Iterable, Mapping

ROOT = pathlib.Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
DEFAULT_TARGET = DATA_DIR / "unified_semantics.sqlite3"
DEFAULT_WORKFLOW = DATA_DIR / "semantic_workflow.sqlite3"
DEFAULT_DUCKDB = DATA_DIR / "semantic_analytics_v155.duckdb"
DEFAULT_METADATA = (
    ROOT.parent
    / "pilots"
    / "metadata"
    / "results"
    / "metadata-semantic-v1-20260815T-v2"
    / "metadata_semantics.sqlite3"
)
DEFAULT_SPEC = ROOT / "pipelines" / "semantic_closure.json"
BACKUP_DIR = ROOT / "backups"

VOLATILE_KEYS = {
    "runid", "createdat", "updatedat", "finishedat", "startedat", "backuppath",
    "manifestpath", "durationseconds", "timestamp", "observedat",
}


def utc_now() -> str:
    """Return the current UTC time in ISO-8601 format."""
    return dt.datetime.now(dt.timezone.utc).isoformat()


def sid(prefix: str, *parts: object) -> str:
    """Build a stable deterministic ID from a prefix and source parts."""
    raw = "|".join("" if part is None else str(part) for part in parts)
    return f"{prefix}-{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:24]}"


def sha256_file(path: pathlib.Path) -> str:
    """Return the SHA-256 of a file without loading the whole file into memory."""
    with path.open("rb") as handle:
        digest = hashlib.sha256()
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_bytes(value: bytes) -> str:
    """Return the SHA-256 of a bytes value."""
    return hashlib.sha256(value).hexdigest()


def connect_readonly(path: pathlib.Path, timeout: float = 30.0) -> sqlite3.Connection:
    """Open a SQLite source in read-only, query-only mode.

    The URI mode prevents an accidental create/write when the path is missing.
    """
    resolved = pathlib.Path(path).resolve()
    if not resolved.exists():
        raise FileNotFoundError(resolved)
    connection = sqlite3.connect(f"{resolved.as_uri()}?mode=ro", uri=True, timeout=timeout)
    connection.execute("PRAGMA query_only=ON")
    connection.row_factory = sqlite3.Row
    return connection


def connect_local(path: pathlib.Path, timeout: float = 30.0) -> sqlite3.Connection:
    """Open a local semantic overlay database with common safe defaults."""
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path), timeout=timeout)
    connection.execute("PRAGMA foreign_keys=ON")
    connection.row_factory = sqlite3.Row
    return connection


def _stable_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _stable_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            if str(key).replace("_", "").lower() not in VOLATILE_KEYS
        }
    if isinstance(value, (list, tuple)):
        return [_stable_value(item) for item in value]
    if isinstance(value, pathlib.Path):
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


def assert_safe_result(result: Mapping[str, Any]) -> None:
    """Enforce the pipeline-wide source-write/publication invariant."""
    unsafe = []
    for key in ("sourceWrite", "source_write", "formalPublication", "formal_publication"):
        if result.get(key) is True or result.get(key) == 1:
            unsafe.append(key)
    if unsafe:
        raise RuntimeError(f"pipeline safety boundary violated: {','.join(unsafe)}")


def write_json_atomic(path: pathlib.Path, payload: Mapping[str, Any]) -> None:
    """Write a manifest atomically so interrupted runs cannot leave JSON half-written."""
    path = pathlib.Path(path)
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


def resolve_artifact_path(value: pathlib.Path | str, run_dir: pathlib.Path | None = None) -> pathlib.Path:
    """Return a manifest artifact path, preferring portable relative paths.

    New manifests store artifact paths relative to their run directory.  Older
    manifests may embed absolute paths from a different checkout location.  This
    helper resolves:

    1. a relative path against ``run_dir`` when provided;
    2. an absolute path that still exists on the current machine;
    3. a stale absolute path by matching the file name under ``run_dir``.

    This keeps verification scripts portable across cloned/moved repositories.
    """
    path = pathlib.Path(value)
    if run_dir is not None and not path.is_absolute():
        candidate = run_dir / path
        if candidate.exists():
            return candidate
    if path.exists():
        return path
    if run_dir is not None:
        candidate = run_dir / path.name
        if candidate.exists():
            return candidate
    return path


def manifest_path(path: pathlib.Path | str, base_dir: pathlib.Path | str) -> str:
    """Return a portable manifest path relative to ``base_dir``.

    New manifests should store artifact paths relative to their own directory so
    the whole repository can be moved or cloned without breaking readers.
    """
    source = pathlib.Path(path)
    base = pathlib.Path(base_dir)
    try:
        return source.resolve().relative_to(base.resolve(), walk_up=True).as_posix()
    except ValueError:
        return str(source)


def backup_before_publish(
    connection: sqlite3.Connection,
    backup_name: str,
    stamp: str | None = None,
    extra_paths: Iterable[pathlib.Path] = (),
    backup_root: pathlib.Path | None = None,
) -> tuple[pathlib.Path, list[pathlib.Path]]:
    """Back up the workflow SQLite DB and optional sidecar files before mutation.

    The backup directory is ``<backup_root>/<backup_name>-pre-<stamp>``.  When
    ``backup_root`` is omitted, ``system/backups`` is used.  The manifest records
    the same safety invariants as the legacy per-script backup helpers: source
    writes and formal publication remain disabled.
    """
    stamp = stamp or utc_now().replace(":", "").replace(".", "")
    root = pathlib.Path(backup_root) if backup_root is not None else BACKUP_DIR
    backup_dir = root / f"{backup_name}-pre-{stamp}"
    backup_dir.mkdir(parents=True, exist_ok=False)
    sqlite_backup = backup_dir / DEFAULT_WORKFLOW.name
    backup_connection = sqlite3.connect(str(sqlite_backup))
    connection.backup(backup_connection)
    backup_connection.close()
    files = [sqlite_backup]
    for source in extra_paths:
        source = pathlib.Path(source)
        if source.exists():
            destination = backup_dir / source.name
            shutil.copy2(source, destination)
            files.append(destination)
    manifest = {
        "status": "pre_publication_backup",
        "created_at_utc": utc_now(),
        "files": [{"path": str(path), "size": path.stat().st_size} for path in files],
        "source_write": False,
        "formal_publication": False,
    }
    (backup_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return backup_dir, files
