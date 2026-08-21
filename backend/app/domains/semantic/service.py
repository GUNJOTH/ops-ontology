"""Canonical RDF read logic for the semantic domain (no HTTP concerns).

Each payload builder takes open read-only connections and returns plain data,
or raises a typed exception the router maps to an HTTP status.  No endpoint
signature, FastAPI dependency or source-table write lives here.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from app.core.canonical_reader import canonical_run_row
from app.core.config import SEMANTIC_CONTEXT_FILE
from app.core.semantic_release import load_active_release

_TYPE_IRI = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
_DEVICE_IRI = "https://semantic.local/ontology/Device"
_CANONICAL_KEY_PREDICATE = "https://semantic.local/ontology/canonicalKey"
_NAMESPACE_PREDICATE = "https://semantic.local/ontology/sourceNamespace"


def canonical_source_of_truth_payload(
    canonical: sqlite3.Connection,
    identity: sqlite3.Connection,
) -> dict[str, Any]:
    """Report the canonical-read cutover coverage without changing data."""
    run = canonical_run_row(canonical)
    projected_devices = 0
    canonical_run_id = None
    if run is not None:
        canonical_run_id = run["run_id"]
        run_manifest = json.loads(run["manifest_json"] or "{}")
        scope = run_manifest.get("canonicalScope") if isinstance(run_manifest, dict) else None
        if isinstance(scope, dict) and scope.get("sourceIdentityDeviceCount") is not None:
            # Full identity projections record the exact count during the
            # streaming write; avoid rescanning tens of millions of local
            # statement/index rows on every UI refresh.
            projected_devices = int(scope["sourceIdentityDeviceCount"])
        else:
            projected_devices = int(canonical.execute(
                """
                SELECT count(DISTINCT subject_iri)
                FROM canonical_statement
                WHERE run_id=? AND predicate_iri=? AND object_iri=?
                """,
                (run["run_id"], _TYPE_IRI, _DEVICE_IRI),
            ).fetchone()[0])
    total_devices = int(identity.execute("SELECT count(*) FROM unified_device").fetchone()[0])
    coverage = (projected_devices / total_devices) if total_devices else 0.0
    return {
        "schemaVersion": "semantic-source-of-truth-v1",
        "status": "active" if total_devices and projected_devices == total_devices else "partial",
        "authority": "Canonical RDF Dataset",
        "canonicalRunId": canonical_run_id,
        "activeRelease": load_active_release(),
        "projectedDeviceCount": projected_devices,
        "identityDeviceCount": total_devices,
        "coverage": round(coverage, 6),
        "canonicalReadRoutes": [
            "/api/semantic/canonical/summary",
            "/api/semantic/canonical/statements",
            "/api/semantic/canonical/device/{source_namespace}/{canonical_key}",
            "/api/semantic/sparql",
            "/api/world-model/device/{unified_device_id}/context (projected objects)",
        ],
        "compatibilityReadRoutes": [
            "/api/world-model/device/{unified_device_id}/context (unprojected objects)",
            "/api/unified-devices*",
        ],
        "controlPlane": ["semantic_* mapping", "approval", "replay", "audit"],
        "sourceWrite": False,
        "formalPublication": False,
    }


def canonical_summary_payload(connection: sqlite3.Connection) -> dict[str, Any]:
    """Return the canonical RDF Dataset run, graphs and safety state."""
    run = canonical_run_row(connection)
    if run is None:
        return {
            "schemaVersion": "canonical-semantic-model-v1",
            "status": "not_initialized",
            "run": None,
            "graphs": [],
            "sourceWrite": False,
            "formalPublication": False,
        }
    graphs = connection.execute(
        "SELECT graph_iri,graph_kind,source_snapshot_id,status FROM canonical_graph WHERE run_id=? ORDER BY graph_kind,graph_iri",
        (run["run_id"],),
    ).fetchall()
    manifest = json.loads(run["manifest_json"] or "{}")
    inference = None
    if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='canonical_inference_run'").fetchone():
        inference_row = connection.execute(
            "SELECT * FROM canonical_inference_run WHERE input_projection_run_id=? ORDER BY created_at DESC LIMIT 1",
            (run["run_id"],),
        ).fetchone()
        inference = dict(inference_row) if inference_row else None
    return {
        "schemaVersion": "canonical-semantic-model-v1",
        "status": run["status"],
        "run": dict(run),
        "standardBaseline": manifest.get("standardBaseline", []),
        "graphs": [dict(row) for row in graphs],
        "counts": {
            "resources": int(run["resource_count"]),
            "statements": int(run["statement_count"]),
            "provenance": int(connection.execute(
                "SELECT count(*) FROM canonical_provenance WHERE statement_id IN (SELECT statement_id FROM canonical_statement WHERE run_id=?)",
                (run["run_id"],),
            ).fetchone()[0]),
            "inferredStatements": int(inference["inferred_statement_count"]) if inference else 0,
        },
        "vocabulary": manifest.get("vocabulary", {}),
        "provenanceCoverage": manifest.get("provenanceCoverage", {}),
        "owlRlReplay": inference,
        "artifacts": manifest.get("artifacts", {}),
        "sourceWrite": False,
        "formalPublication": False,
    }


def canonical_statements_payload(
    connection: sqlite3.Connection,
    subject_iri: str | None,
    predicate_iri: str | None,
    limit: int,
) -> dict[str, Any]:
    """Read canonical RDF statements without exposing the source tables."""
    run = canonical_run_row(connection)
    if run is None:
        return {
            "schemaVersion": "canonical-semantic-model-v1",
            "runId": None,
            "rows": [],
            "sourceWrite": False,
            "formalPublication": False,
        }
    where = ["run_id=?"]
    params: list[Any] = [run["run_id"]]
    if subject_iri:
        where.append("subject_iri=?")
        params.append(subject_iri)
    if predicate_iri:
        where.append("predicate_iri=?")
        params.append(predicate_iri)
    rows = connection.execute(
        f"SELECT graph_id,subject_iri,predicate_iri,object_kind,object_iri,lexical_value,datatype_iri,language_tag,confidence,assertion_status FROM canonical_statement WHERE {' AND '.join(where)} ORDER BY subject_iri,predicate_iri,statement_id LIMIT ?",
        [*params, limit],
    ).fetchall()
    inferred_rows: list[dict[str, Any]] = []
    if not subject_iri and not predicate_iri and connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='canonical_inference_run'").fetchone():
        inference = connection.execute(
            "SELECT run_id,graph_iri FROM canonical_inference_run WHERE input_projection_run_id=? AND status='completed' ORDER BY created_at DESC LIMIT 1",
            (run["run_id"],),
        ).fetchone()
        if inference:
            inferred_rows = [
                {**dict(row), "graph_id": inference["graph_iri"], "assertion_status": "inferred", "confidence": None}
                for row in connection.execute(
                    "SELECT subject_iri,predicate_iri,object_kind,object_iri,lexical_value,datatype_iri,language_tag FROM canonical_inferred_statement WHERE run_id=? ORDER BY subject_iri,predicate_iri,statement_id LIMIT ?",
                    (inference["run_id"], max(0, limit - len(rows))),
                ).fetchall()
            ]
    return {
        "schemaVersion": "canonical-semantic-model-v1",
        "runId": run["run_id"],
        "rows": [dict(row) for row in rows] + inferred_rows,
        "sourceWrite": False,
        "formalPublication": False,
    }


def canonical_device_payload(
    connection: sqlite3.Connection,
    source_namespace: str,
    canonical_key: str,
) -> dict[str, Any]:
    """Return one device as JSON-LD-compatible data plus provenance.

    Raises ``LookupError`` when the canonical model is unprojected or the
    device is unknown; ``OSError``/``json.JSONDecodeError`` when the JSON-LD
    context file is unreadable.  The router maps these to HTTP statuses.
    """
    run = canonical_run_row(connection)
    if run is None:
        raise LookupError("Canonical Semantic Model 尚未完成投影")
    subjects = connection.execute(
        """SELECT DISTINCT key_stmt.subject_iri
           FROM canonical_statement key_stmt
           JOIN canonical_statement namespace_stmt
             ON namespace_stmt.run_id=key_stmt.run_id AND namespace_stmt.subject_iri=key_stmt.subject_iri
           WHERE key_stmt.run_id=? AND key_stmt.predicate_iri=? AND key_stmt.lexical_value=?
             AND namespace_stmt.predicate_iri=? AND namespace_stmt.lexical_value=?""",
        (run["run_id"], _CANONICAL_KEY_PREDICATE, canonical_key, _NAMESPACE_PREDICATE, source_namespace),
    ).fetchall()
    if not subjects:
        raise LookupError("Canonical Semantic Model 中不存在该设备")
    subject = subjects[0]["subject_iri"]
    rows = connection.execute(
        "SELECT statement_id,predicate_iri,object_kind,object_iri,lexical_value,datatype_iri,language_tag,confidence,assertion_status FROM canonical_statement WHERE run_id=? AND subject_iri=? ORDER BY predicate_iri,statement_id",
        (run["run_id"], subject),
    ).fetchall()
    document: dict[str, Any] = {"@id": subject, "@type": [], "@graphSource": "canonical-rdf-dataset"}
    provenance: list[dict[str, Any]] = []
    for row in rows:
        predicate = row["predicate_iri"]
        if predicate == _TYPE_IRI:
            document["@type"].append(row["object_iri"])
            continue
        if row["object_kind"] == "iri":
            value: Any = {"@id": row["object_iri"]}
        else:
            value = {"@value": row["lexical_value"]}
            if row["datatype_iri"]:
                value["@type"] = row["datatype_iri"]
            if row["language_tag"]:
                value["@language"] = row["language_tag"]
        document.setdefault(predicate, []).append(value)
        prov_rows = connection.execute(
            "SELECT source_system,source_schema,source_table,source_row_id,source_snapshot_id,activity_type,activity_id,evidence_json FROM canonical_provenance WHERE statement_id=? ORDER BY created_at",
            (row["statement_id"],),
        ).fetchall()
        provenance.extend(dict(item) for item in prov_rows)
    for key, value in list(document.items()):
        if isinstance(value, list) and len(value) == 1:
            document[key] = value[0]
    context_payload = json.loads(SEMANTIC_CONTEXT_FILE.read_text(encoding="utf-8"))
    jsonld_context = context_payload.get("@context", context_payload)
    return {
        "schemaVersion": "canonical-semantic-model-v1",
        "runId": run["run_id"],
        "jsonld": {"@context": jsonld_context, **document},
        "provenance": provenance,
        "sourceWrite": False,
        "formalPublication": False,
    }


def sparql_payload(connection: sqlite3.Connection, query: str) -> dict[str, Any]:
    """Execute a bounded read-only SPARQL 1.1 SELECT/ASK on the latest graph.

    Raises ``ImportError`` when rdflib is missing, ``LookupError`` when the
    canonical model is not projected, and any rdflib error for invalid query
    shapes (mapped to 4xx/5xx by the router).
    """
    from rdflib import Dataset, Literal, URIRef

    run = canonical_run_row(connection)
    if run is None:
        raise LookupError("Canonical Semantic Model 尚未完成投影")
    dataset = Dataset()
    default_graph = dataset.default_context
    for row in connection.execute(
        "SELECT graph_id,subject_iri,predicate_iri,object_kind,object_iri,lexical_value,datatype_iri,language_tag FROM canonical_statement WHERE run_id=?",
        (run["run_id"],),
    ):
        graph_iri = connection.execute("SELECT graph_iri FROM canonical_graph WHERE graph_id=?", (row["graph_id"],)).fetchone()[0]
        graph = dataset.graph(URIRef(graph_iri))
        subject = URIRef(row["subject_iri"])
        predicate = URIRef(row["predicate_iri"])
        if row["object_kind"] == "iri":
            obj = URIRef(row["object_iri"])
        else:
            obj = Literal(
                row["lexical_value"] or "",
                datatype=URIRef(row["datatype_iri"]) if row["datatype_iri"] else None,
                lang=row["language_tag"],
            )
        graph.add((subject, predicate, obj))
        # The endpoint exposes the union as the SPARQL default graph while
        # retaining named graphs for callers that need source/derived
        # provenance boundaries.
        default_graph.add((subject, predicate, obj))
    if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='canonical_inference_run'").fetchone():
        inference = connection.execute(
            "SELECT run_id FROM canonical_inference_run WHERE input_projection_run_id=? AND status='completed' ORDER BY created_at DESC LIMIT 1",
            (run["run_id"],),
        ).fetchone()
        if inference:
            for row in connection.execute(
                "SELECT graph_iri,subject_iri,predicate_iri,object_kind,object_iri,lexical_value,datatype_iri,language_tag FROM canonical_inferred_statement WHERE run_id=?",
                (inference["run_id"],),
            ):
                graph = dataset.graph(URIRef(row["graph_iri"]))
                subject = URIRef(row["subject_iri"])
                predicate = URIRef(row["predicate_iri"])
                obj = URIRef(row["object_iri"]) if row["object_kind"] == "iri" else Literal(
                    row["lexical_value"] or "",
                    datatype=URIRef(row["datatype_iri"]) if row["datatype_iri"] else None,
                    lang=row["language_tag"],
                )
                graph.add((subject, predicate, obj))
                default_graph.add((subject, predicate, obj))
    result = dataset.query(query)
    if result.type == "ASK":
        return {
            "schemaVersion": "sparql-1.1",
            "runId": run["run_id"],
            "type": "ASK",
            "boolean": bool(result.askAnswer),
            "sourceWrite": False,
            "formalPublication": False,
        }
    bindings: list[dict[str, Any]] = []
    for row in result:
        if len(bindings) >= 500:
            break
        item: dict[str, Any] = {}
        for variable, value in row.asdict().items():
            if value is None:
                continue
            if isinstance(value, URIRef):
                item[str(variable)] = {"@id": str(value)}
            elif isinstance(value, Literal):
                item[str(variable)] = {
                    "@value": str(value),
                    **({"@type": str(value.datatype)} if value.datatype else {}),
                    **({"@language": value.language} if value.language else {}),
                }
            else:
                item[str(variable)] = str(value)
        bindings.append(item)
    return {
        "schemaVersion": "sparql-1.1",
        "runId": run["run_id"],
        "type": "SELECT",
        "vars": [str(value) for value in result.vars],
        "bindings": bindings,
        "truncated": len(bindings) >= 500,
        "sourceWrite": False,
        "formalPublication": False,
    }
