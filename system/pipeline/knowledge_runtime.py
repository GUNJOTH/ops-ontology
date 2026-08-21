"""Read-only contracts for the enterprise knowledge runtime.

The relational knowledge tables are a control-plane index.  This module keeps
the lifecycle vocabulary, production eligibility and release checks in one
place so API handlers, build scripts and CI do not invent different meanings.
It never writes a source system or promotes a knowledge asset.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from rdflib import Graph
from rdflib.namespace import OWL, RDF
from semantic_registry import object_class_local_name

KNOWLEDGE_ASSET_TYPES: tuple[str, ...] = (
    "document", "standard", "rule", "sop", "case", "expert", "fragment",
    "terminology", "evaluation_case", "definition",
)
LIFECYCLE_STATES: tuple[str, ...] = (
    "Draft", "Candidate", "UnderReview", "Approved", "Published", "Deprecated", "Archived",
)
PRODUCTION_STATUSES: frozenset[str] = frozenset({"published", "enabled"})
NON_PRODUCTION_STATUSES: frozenset[str] = frozenset({"draft", "proposed", "replayed", "approved", "needs_review"})
TERMINAL_STATUSES: frozenset[str] = frozenset({"retired", "deprecated", "archived", "blocked", "rejected"})

_LIFECYCLE_TO_STORAGE = {
    "Draft": "draft",
    "Candidate": "proposed",
    "UnderReview": "needs_review",
    "Approved": "approved",
    "Published": "published",
    "Deprecated": "deprecated",
    "Archived": "archived",
}


def lifecycle_to_storage(value: str | None) -> str:
    """Normalize the public lifecycle vocabulary to the local status index."""
    candidate = str(value or "").strip()
    if candidate in _LIFECYCLE_TO_STORAGE:
        return _LIFECYCLE_TO_STORAGE[candidate]
    return candidate.lower()


def storage_to_lifecycle(value: str | None) -> str:
    """Return the stable public lifecycle name for legacy or V1 statuses."""
    candidate = str(value or "").strip().lower()
    reverse = {value: key for key, value in _LIFECYCLE_TO_STORAGE.items()}
    return reverse.get(candidate, {
        "enabled": "Published",
        "retired": "Deprecated",
        "registered": "Draft",
        "replayed": "Candidate",
        "blocked": "Archived",
    }.get(candidate, candidate or "Draft"))


def is_production_knowledge(status: str | None, lifecycle_status: str | None = None) -> bool:
    """Only released knowledge is eligible for default Agent context."""
    status_value = str(status or "").strip().lower()
    lifecycle_value = str(lifecycle_status or "").strip()
    return status_value in PRODUCTION_STATUSES or lifecycle_value == "Published"


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    if not _table_exists(connection, table):
        return set()
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})").fetchall()}


def ensure_knowledge_columns(connection: sqlite3.Connection) -> None:
    """Add V3 registry metadata to an existing local overlay in place.

    This is deliberately additive.  Existing V1/V2 rows and their status
    values remain valid; the public lifecycle is carried by
    ``lifecycle_status`` until a separately approved migration changes the
    legacy CHECK constraint.
    """
    if not _table_exists(connection, "knowledge_asset"):
        return
    additions = {
        "knowledge_kind": "TEXT NOT NULL DEFAULT 'legacy'",
        "package_id": "TEXT NOT NULL DEFAULT 'platform-core'",
        "knowledge_domain": "TEXT NOT NULL DEFAULT 'enterprise-operations'",
        "source_type": "TEXT",
        "source_id": "TEXT",
        "source_uri": "TEXT",
        "owner": "TEXT",
        "reviewer": "TEXT",
        "confidence": "REAL",
        "valid_from": "TEXT",
        "valid_to": "TEXT",
        "ontology_version": "TEXT",
        "quality_score": "REAL",
        "lifecycle_status": "TEXT NOT NULL DEFAULT 'Draft'",
        "published_at": "TEXT",
        "deprecated_at": "TEXT",
    }
    existing = _columns(connection, "knowledge_asset")
    for name, definition in additions.items():
        if name not in existing:
            connection.execute(f"ALTER TABLE knowledge_asset ADD COLUMN {name} {definition}")
    version_additions = {
        "ontology_version": "TEXT",
        "extraction_model": "TEXT",
        "prompt_version": "TEXT",
        "valid_from": "TEXT",
        "valid_to": "TEXT",
        "release_id": "TEXT",
    }
    for name, definition in version_additions.items():
        if name not in _columns(connection, "knowledge_asset_version"):
            connection.execute(f"ALTER TABLE knowledge_asset_version ADD COLUMN {name} {definition}")
    source_additions = {
        "source_uri": "TEXT",
        "provenance_role": "TEXT",
        "page_number": "INTEGER",
        "section_path": "TEXT",
        "fragment_hash": "TEXT",
    }
    for name, definition in source_additions.items():
        if name not in _columns(connection, "knowledge_asset_source"):
            connection.execute(f"ALTER TABLE knowledge_asset_source ADD COLUMN {name} {definition}")
    binding_additions = {"target_iri": "TEXT", "package_id": "TEXT", "ontology_version": "TEXT"}
    for name, definition in binding_additions.items():
        if name not in _columns(connection, "knowledge_asset_binding"):
            connection.execute(f"ALTER TABLE knowledge_asset_binding ADD COLUMN {name} {definition}")
    if "conflict_key" not in _columns(connection, "knowledge_asset_issue"):
        connection.execute("ALTER TABLE knowledge_asset_issue ADD COLUMN conflict_key TEXT")
    connection.execute(
        """UPDATE knowledge_asset
           SET lifecycle_status=CASE lower(status)
             WHEN 'enabled' THEN 'Published'
             WHEN 'retired' THEN 'Deprecated'
             WHEN 'approved' THEN 'Approved'
             WHEN 'needs_review' THEN 'UnderReview'
             WHEN 'proposed' THEN 'Candidate'
             WHEN 'blocked' THEN 'Archived'
             ELSE 'Draft' END
           WHERE lifecycle_status IS NULL OR trim(lifecycle_status)='' OR lifecycle_status='Draft' AND lower(status)='enabled'"""
    )


def _load_ontology_classes(path: Path | None) -> set[str]:
    if path is None or not path.exists():
        return set()
    graph = Graph()
    graph.parse(str(path), format="turtle")
    return {str(item) for item in graph.subjects(RDF.type, OWL.Class)}


def _as_of(value: str | None) -> datetime:
    if value:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    return datetime.now(timezone.utc)


def _expired(value: str | None, as_of: datetime) -> bool:
    if not value:
        return False
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return True
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed <= as_of


def knowledge_summary_payload(connection: sqlite3.Connection) -> dict[str, Any]:
    """Return a bounded registry summary suitable for the UI and Agent tools."""
    if not _table_exists(connection, "knowledge_asset"):
        return {"status": "not_initialized", "assetCount": 0, "sourceWrite": False, "formalPublication": False}
    run = connection.execute(
        "SELECT * FROM knowledge_layer_run ORDER BY created_at DESC LIMIT 1"
    ).fetchone() if _table_exists(connection, "knowledge_layer_run") else None
    by_type = [dict(row) for row in connection.execute(
        "SELECT asset_type AS value,count(*) AS count FROM knowledge_asset GROUP BY asset_type ORDER BY asset_type"
    ).fetchall()]
    by_status = [dict(row) for row in connection.execute(
        "SELECT status AS value,count(*) AS count FROM knowledge_asset GROUP BY status ORDER BY status"
    ).fetchall()]
    open_conflicts = int(connection.execute(
        "SELECT count(*) FROM knowledge_asset_issue WHERE issue_type='conflict' AND status='open'"
    ).fetchone()[0]) if _table_exists(connection, "knowledge_asset_issue") else 0
    return {
        "status": "ready" if run else "not_built",
        "runId": run["run_id"] if run else None,
        "assetCount": int(connection.execute("SELECT count(*) FROM knowledge_asset").fetchone()[0]),
        "versionCount": int(connection.execute("SELECT count(*) FROM knowledge_asset_version").fetchone()[0]) if _table_exists(connection, "knowledge_asset_version") else 0,
        "sourceCount": int(connection.execute("SELECT count(*) FROM knowledge_asset_source").fetchone()[0]) if _table_exists(connection, "knowledge_asset_source") else 0,
        "publishedCount": int(connection.execute("SELECT count(*) FROM knowledge_asset WHERE lower(status) IN ('published','enabled')").fetchone()[0]),
        "openConflictCount": open_conflicts,
        "assetTypes": by_type,
        "statuses": by_status,
        "sourceWrite": False,
        "formalPublication": False,
    }


def validate_knowledge_layer(
    connection: sqlite3.Connection,
    ontology_path: Path | None = None,
    as_of: str | None = None,
) -> dict[str, Any]:
    """Validate release-critical knowledge invariants without changing data."""
    failures: list[str] = []
    counts: dict[str, int] = {}
    if not _table_exists(connection, "knowledge_asset"):
        return {"status": "FAIL", "failures": ["KNOWLEDGE_ASSET_TABLE_MISSING"], "counts": counts, "sourceWrite": False, "formalPublication": False}
    columns = _columns(connection, "knowledge_asset")
    version_exists = _table_exists(connection, "knowledge_asset_version")
    source_exists = _table_exists(connection, "knowledge_asset_source")
    binding_exists = _table_exists(connection, "knowledge_asset_binding")
    issue_exists = _table_exists(connection, "knowledge_asset_issue")
    counts["assets"] = int(connection.execute("SELECT count(*) FROM knowledge_asset").fetchone()[0])
    counts["versions"] = int(connection.execute("SELECT count(*) FROM knowledge_asset_version").fetchone()[0]) if version_exists else 0
    counts["sources"] = int(connection.execute("SELECT count(*) FROM knowledge_asset_source").fetchone()[0]) if source_exists else 0
    counts["bindings"] = int(connection.execute("SELECT count(*) FROM knowledge_asset_binding").fetchone()[0]) if binding_exists else 0
    counts["published"] = int(connection.execute("SELECT count(*) FROM knowledge_asset WHERE lower(status) IN ('published','enabled')").fetchone()[0])
    now = _as_of(as_of)

    ontology_classes = _load_ontology_classes(ontology_path)
    assets = connection.execute("SELECT * FROM knowledge_asset ORDER BY asset_id").fetchall()
    for asset in assets:
        status = str(asset["status"] or "")
        lifecycle = str(asset["lifecycle_status"] or "") if "lifecycle_status" in columns else ""
        production = is_production_knowledge(status, lifecycle)
        if not production:
            continue
        asset_id = str(asset["asset_id"])
        if _expired(asset["valid_to"] if "valid_to" in columns else None, now):
            failures.append(f"KNOWLEDGE_EXPIRED:{asset_id}")
        if version_exists:
            version = connection.execute(
                "SELECT asset_version_id FROM knowledge_asset_version WHERE asset_id=? AND version=? LIMIT 1",
                (asset_id, asset["current_version"]),
            ).fetchone()
            if version is None:
                failures.append(f"KNOWLEDGE_CURRENT_VERSION_MISSING:{asset_id}")
        if source_exists:
            source = connection.execute(
                "SELECT 1 FROM knowledge_asset_source WHERE asset_id=? AND (source_snapshot_id IS NOT NULL AND trim(source_snapshot_id)<>'') LIMIT 1",
                (asset_id,),
            ).fetchone()
            if source is None:
                failures.append(f"KNOWLEDGE_PROVENANCE_MISSING:{asset_id}")
        else:
            failures.append(f"KNOWLEDGE_SOURCE_TABLE_MISSING:{asset_id}")
        if issue_exists and connection.execute(
            "SELECT 1 FROM knowledge_asset_issue WHERE asset_id=? AND issue_type='conflict' AND status='open' AND severity IN ('medium','high') LIMIT 1",
            (asset_id,),
        ).fetchone() is not None:
            failures.append(f"KNOWLEDGE_CONFLICT_OPEN:{asset_id}")

    if binding_exists:
        binding_columns = _columns(connection, "knowledge_asset_binding")
        binding_select = "binding_id,object_type,target_iri,status" if "target_iri" in binding_columns else "binding_id,object_type,status"
        for row in connection.execute(
            f"SELECT {binding_select} FROM knowledge_asset_binding WHERE status='accepted' ORDER BY binding_id"
        ).fetchall():
            object_type = str(row["object_type"] or "")
            iri = str(row["target_iri"] or "").strip() if "target_iri" in binding_columns else ""
            if not iri:
                local = object_class_local_name(object_type)
                iri = "https://semantic.local/ontology/" + local
            if ontology_classes and iri not in ontology_classes:
                failures.append(f"KNOWLEDGE_BINDING_UNKNOWN_CLASS:{row['binding_id']}:{object_type}")

    return {
        "status": "PASS" if not failures else "FAIL",
        "failures": failures,
        "counts": counts,
        "policy": [
            "Agent 默认只读取 Published/enabled 且未过期、无未解决中高危冲突的知识",
            "Published 知识必须有当前版本和 source_snapshot_id 证据",
            "appliesTo 绑定必须能解析到 Canonical Ontology Class",
        ],
        "sourceWrite": False,
        "formalPublication": False,
    }
