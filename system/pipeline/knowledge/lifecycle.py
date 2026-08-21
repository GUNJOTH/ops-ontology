"""Knowledge candidate review and release operations."""
from __future__ import annotations

import hashlib
import json
import pathlib
import sqlite3
from datetime import datetime, timezone
from typing import Any

from rdflib import Graph
from rdflib.namespace import OWL, RDF

from .documents import _insert_asset


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _release_root() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parents[3] / "reports" / "knowledge-releases"


def _ontology_path() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parents[3] / "standards" / "v2" / "ontology.ttl"


def _target_iri(value: object) -> str:
    text = str(value or "").strip()
    return text if text.startswith("http://") or text.startswith("https://") else "https://semantic.local/ontology/" + text


def _ontology_class_exists(value: object) -> bool:
    path = _ontology_path()
    if not path.exists():
        return False
    graph = Graph()
    graph.parse(str(path), format="turtle")
    target = _target_iri(value)
    return graph.value(subject=None, predicate=RDF.type, object=OWL.Class) is not None and any(str(subject) == target for subject in graph.subjects(RDF.type, OWL.Class))


def create_candidate(
    connection: sqlite3.Connection,
    *,
    title: str,
    definition: dict[str, Any],
    source_fragment_id: str,
    knowledge_kind: str = "expert",
    package_id: str = "platform-core",
    domain: str = "enterprise-operations",
    extraction_model: str = "deterministic-v1",
    prompt_version: str = "none",
) -> dict[str, Any]:
    """Persist one candidate; it can never enter a released set directly."""
    now = _now()
    canonical = json.dumps(definition, ensure_ascii=False, sort_keys=True)
    asset_key = "CANDIDATE:" + hashlib.sha256((source_fragment_id + "|" + canonical).encode("utf-8")).hexdigest()[:32]
    asset_id, version_id = _insert_asset(
        connection, asset_key=asset_key, asset_type="expert", knowledge_kind=knowledge_kind,
        title=title, definition=canonical, version="v1", package_id=package_id, domain=domain,
        source_id=source_fragment_id, source_uri="", now=now,
    )
    connection.execute(
        "UPDATE knowledge_asset_version SET definition_json=? WHERE asset_version_id=?",
        (canonical, version_id),
    )
    connection.execute(
        "UPDATE knowledge_asset SET status='proposed',lifecycle_status='Candidate',confidence=?,quality_score=?,updated_at=? WHERE asset_id=?",
        (float(definition.get("confidence") or 0), float(definition.get("qualityScore") or 0), now, asset_id),
    )
    connection.execute(
        "UPDATE knowledge_asset_version SET status='proposed',extraction_model=?,prompt_version=? WHERE asset_version_id=?",
        (extraction_model, prompt_version, version_id),
    )
    connection.execute(
        "INSERT OR IGNORE INTO knowledge_asset_source(source_id,asset_id,source_kind,source_record_id,source_table,source_snapshot_id,source_status,evidence_json,provenance_role,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("KAS-" + hashlib.sha256((asset_id + source_fragment_id).encode("utf-8")).hexdigest()[:24], asset_id,
         "document_fragment", source_fragment_id, "knowledge_asset", "local-document-import", "read_only",
         json.dumps({"fragmentAssetId": source_fragment_id, "extractionModel": extraction_model}, ensure_ascii=False),
         "candidate-extraction", now),
    )
    applies_to = definition.get("appliesToClass") or definition.get("appliesTo")
    if applies_to:
        connection.execute(
            "INSERT OR IGNORE INTO knowledge_asset_binding(binding_id,asset_id,object_type,object_key,relation_type,status,evidence_json,target_iri,package_id,ontology_version,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            ("KAB-" + hashlib.sha256((asset_id + str(applies_to)).encode("utf-8")).hexdigest()[:24], asset_id, "business_object", str(applies_to), "appliesToClass", "needs_review", json.dumps({"sourceFragmentId": source_fragment_id}, ensure_ascii=False), _target_iri(applies_to), package_id, "enterprise-operations-ontology/v2", now),
        )
    connection.commit()
    return {"assetId": asset_id, "assetVersionId": version_id, "status": "Candidate", "sourceWrite": False, "formalPublication": False}


def _ensure_review_table(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS knowledge_asset_review(
          review_id TEXT PRIMARY KEY,asset_id TEXT NOT NULL,idempotency_key TEXT NOT NULL UNIQUE,
          decision TEXT NOT NULL,reviewer TEXT NOT NULL,note TEXT NOT NULL,created_at TEXT NOT NULL
        )
        """
    )


def approve_knowledge(
    connection: sqlite3.Connection,
    asset_id: str,
    *,
    decision: str,
    reviewer: str,
    note: str,
    idempotency_key: str,
) -> dict[str, Any]:
    """Record an explicit local review without publishing the asset."""
    _ensure_review_table(connection)
    asset = connection.execute("SELECT * FROM knowledge_asset WHERE asset_id=?", (asset_id,)).fetchone()
    if asset is None:
        raise LookupError("知识资产不存在")
    prior = connection.execute("SELECT * FROM knowledge_asset_review WHERE idempotency_key=?", (idempotency_key,)).fetchone()
    if prior is not None:
        return {"status": "already_reviewed", "review": dict(prior), "sourceWrite": False, "formalPublication": False}
    now = _now()
    review_id = "KAR-" + hashlib.sha256((asset_id + idempotency_key).encode("utf-8")).hexdigest()[:24]
    normalized = decision.strip().lower()
    if normalized not in {"approved", "rejected"}:
        raise ValueError("知识审核决定必须是 approved 或 rejected")
    status, lifecycle = ("approved", "Approved") if normalized == "approved" else ("blocked", "Archived")
    bindings = connection.execute("SELECT binding_id,target_iri,status FROM knowledge_asset_binding WHERE asset_id=?", (asset_id,)).fetchall()
    if normalized == "approved":
        invalid = [row["binding_id"] for row in bindings if row["target_iri"] and not _ontology_class_exists(row["target_iri"])]
        if invalid:
            raise ValueError(f"知识绑定未命中 Canonical Ontology Class：{','.join(invalid)}")
    connection.execute("INSERT INTO knowledge_asset_review VALUES (?,?,?,?,?,?,?)", (review_id, asset_id, idempotency_key, normalized, reviewer, note, now))
    connection.execute("UPDATE knowledge_asset SET status=?,lifecycle_status=?,reviewer=?,updated_at=? WHERE asset_id=?", (status, lifecycle, reviewer, now, asset_id))
    if normalized == "approved":
        connection.execute("UPDATE knowledge_asset_binding SET status='accepted' WHERE asset_id=? AND status='needs_review'", (asset_id,))
    connection.execute("UPDATE knowledge_asset_version SET status=? WHERE asset_id=? AND version=?", (status, asset_id, asset["current_version"]))
    connection.commit()
    return {"status": normalized, "reviewId": review_id, "assetId": asset_id, "sourceWrite": False, "formalPublication": False}


def _release_candidates(connection: sqlite3.Connection) -> tuple[list[sqlite3.Row], list[dict[str, Any]]]:
    rows = connection.execute("SELECT * FROM knowledge_asset WHERE status='approved' ORDER BY asset_key").fetchall()
    failures: list[dict[str, Any]] = []
    if not rows:
        failures.append({"code": "NO_APPROVED_KNOWLEDGE"})
    for row in rows:
        version = connection.execute("SELECT * FROM knowledge_asset_version WHERE asset_id=? AND version=?", (row["asset_id"], row["current_version"])).fetchone()
        sources = connection.execute("SELECT count(*) FROM knowledge_asset_source WHERE asset_id=? AND source_snapshot_id IS NOT NULL", (row["asset_id"],)).fetchone()[0]
        if version is None:
            failures.append({"assetId": row["asset_id"], "code": "CURRENT_VERSION_MISSING"})
        if not sources:
            failures.append({"assetId": row["asset_id"], "code": "SOURCE_EVIDENCE_MISSING"})
        conflicts = connection.execute(
            "SELECT count(*) FROM knowledge_asset_issue WHERE asset_id=? AND issue_type='conflict' AND status='open' AND severity IN ('medium','high')",
            (row["asset_id"],),
        ).fetchone()[0]
        if conflicts:
            failures.append({"assetId": row["asset_id"], "code": "OPEN_KNOWLEDGE_CONFLICT"})
        definition = json.loads(version["definition_json"]) if version is not None else {}
        applies_to = definition.get("appliesToClass") or definition.get("appliesTo") if isinstance(definition, dict) else None
        if applies_to:
            binding = connection.execute(
                "SELECT 1 FROM knowledge_asset_binding WHERE asset_id=? AND relation_type='appliesToClass' AND status='accepted' AND target_iri=? LIMIT 1",
                (row["asset_id"], _target_iri(applies_to)),
            ).fetchone()
            if binding is None:
                failures.append({"assetId": row["asset_id"], "code": "ONTOLOGY_BINDING_NOT_ACCEPTED"})
    return rows, failures


def release_approved_knowledge(
    connection: sqlite3.Connection,
    *,
    release_id: str,
    reviewer: str,
    note: str = "",
) -> dict[str, Any]:
    """Release approved, evidence-backed knowledge into the local set."""
    rows, failures = _release_candidates(connection)
    if failures:
        return {"status": "blocked", "releaseId": release_id, "candidates": len(rows), "failures": failures, "sourceWrite": False, "formalPublication": False}
    now = _now()
    connection.execute("BEGIN")
    try:
        for row in rows:
            connection.execute("UPDATE knowledge_asset SET status='published',lifecycle_status='Published',published_at=?,updated_at=? WHERE asset_id=?", (now, now, row["asset_id"]))
            connection.execute("UPDATE knowledge_asset_version SET status='published',release_id=? WHERE asset_id=? AND version=?", (release_id, row["asset_id"], row["current_version"]))
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    payload = {
        "schemaVersion": "knowledge-release-v1", "releaseId": release_id, "releasedAt": now,
        "reviewer": reviewer, "note": note, "ontologyVersion": "enterprise-operations-ontology/v2",
        "assetIds": [row["asset_id"] for row in rows], "assetCount": len(rows),
        "sourceWrite": False, "formalPublication": False,
    }
    payload["contentHash"] = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    output = _release_root()
    output.mkdir(parents=True, exist_ok=True)
    manifest = output / f"{release_id}.json"
    manifest.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"status": "released", "releaseId": release_id, "assetCount": len(rows), "manifestPath": str(manifest), "sourceWrite": False, "formalPublication": False}
