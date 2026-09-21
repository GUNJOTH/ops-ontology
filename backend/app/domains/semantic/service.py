"""Canonical RDF read logic for the semantic domain (no HTTP concerns).

Each payload builder takes open read-only connections and returns plain data,
or raises a typed exception the router maps to an HTTP status.  No endpoint
signature, FastAPI dependency or source-table write lives here.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from ontology_package import PackageResolver
from rdflib import Graph
from rdflib.namespace import OWL, RDF, RDFS
from semantic_packages import package_summary
from semantic_registry import object_class_local_name

from app.core.canonical_reader import canonical_run_row
from app.core.config import PROJECT_ROOT, SEMANTIC_CONTEXT_FILE
from app.core.semantic_release import load_active_release
from app.domains.knowledge_assets.service import knowledge_for_object_payload

_TYPE_IRI = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
_DEVICE_IRI = "https://semantic.local/ontology/Device"
_CANONICAL_KEY_PREDICATE = "https://semantic.local/ontology/canonicalKey"
_NAMESPACE_PREDICATE = "https://semantic.local/ontology/sourceNamespace"
_HAS_EVENT_PREDICATE = "https://semantic.local/ontology/hasEvent"
_HAS_FACT_PREDICATE = "https://semantic.local/ontology/hasFact"
_HAS_CURRENT_STATE_PREDICATE = "https://semantic.local/ontology/hasCurrentState"
_RDF_TYPE = _TYPE_IRI
_PROV_PREFIX = "http://www.w3.org/ns/prov#"
_ONTOLOGY_NAMESPACE = "https://semantic.local/ontology/"
_OCCURRED_AT_PREDICATE = _ONTOLOGY_NAMESPACE + "occurredAt"
_RECORDED_AT_PREDICATE = _ONTOLOGY_NAMESPACE + "recordedAt"
_STATUS_PREDICATE = _ONTOLOGY_NAMESPACE + "status"
_DISPLAY_NAME_PREDICATE = _ONTOLOGY_NAMESPACE + "displayName"


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def semantic_packages_payload() -> dict[str, Any]:
    """Return the product package boundary and its read-only gate status."""
    return package_summary()


def semantic_package_payload(package_id: str) -> dict[str, Any]:
    """Return one resolved native package and its declared assets."""
    package = PackageResolver().resolve(package_id)
    assets: dict[str, Any] = {}
    for name in ("ontology", "shapes", "vocabularies", "context", "mappings", "rules", "events", "stateMachines", "actions", "tests"):
        path = package.asset(name)
        assets[name] = {"path": str(path.relative_to(package.root)), "exists": path.exists()}
    return {
        "schemaVersion": "semantic-api-v2",
        "id": package.package_id,
        "version": package.version,
        "type": package.package_type,
        "namespace": package.manifest.get("namespace"),
        "ontologyNamespace": package.manifest.get("ontologyNamespace"),
        "dependsOn": package.manifest.get("dependsOn", []),
        "ownedClasses": package.manifest.get("ownedClasses", []),
        "ownedObjectProperties": package.manifest.get("ownedObjectProperties", []),
        "ownedDatatypeProperties": package.manifest.get("ownedDatatypeProperties", []),
        "assets": assets,
        "sourceWrite": False,
        "formalPublication": False,
    }


def semantic_ontology_catalog_payload() -> dict[str, Any]:
    """Expose the composed OWL class/property catalog for reviewers and Agents."""
    registry = PackageResolver().registry
    ontology_asset = str(registry.get("canonicalOntology", "standards/v2/ontology.ttl"))
    ontology_path = (PROJECT_ROOT / ontology_asset).resolve()
    graph = Graph()
    graph.parse(str(ontology_path), format="turtle")

    def local(iri: object) -> str:
        value = str(iri)
        return value[len(_ONTOLOGY_NAMESPACE):] if value.startswith(_ONTOLOGY_NAMESPACE) else value

    classes = []
    for subject in sorted(graph.subjects(RDF.type, OWL.Class), key=str):
        if not str(subject).startswith(_ONTOLOGY_NAMESPACE):
            continue
        parents = sorted(local(parent) for parent in graph.objects(subject, RDFS.subClassOf))
        classes.append({"iri": str(subject), "name": local(subject), "parents": parents})
    object_properties = []
    for subject in sorted(graph.subjects(RDF.type, OWL.ObjectProperty), key=str):
        if not str(subject).startswith(_ONTOLOGY_NAMESPACE):
            continue
        object_properties.append({"iri": str(subject), "name": local(subject)})
    datatype_properties = []
    for subject in sorted(graph.subjects(RDF.type, OWL.DatatypeProperty), key=str):
        if not str(subject).startswith(_ONTOLOGY_NAMESPACE):
            continue
        datatype_properties.append({"iri": str(subject), "name": local(subject)})
    return {
        "schemaVersion": "semantic-api-v2",
        "ontologyVersion": "enterprise-operations-ontology/v2",
        "ontology": ontology_asset,
        "classes": classes,
        "objectProperties": object_properties,
        "datatypeProperties": datatype_properties,
        "sourceWrite": False,
        "formalPublication": False,
    }


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


def canonical_object_payload(
    connection: sqlite3.Connection,
    object_type: str,
    canonical_key: str,
) -> dict[str, Any]:
    """Return a generic semantic object context from the Canonical Dataset.

    This is intentionally a read-only, bounded API.  It exposes references to
    events, facts, states and rules; callers can follow each reference through
    the same object endpoint instead of receiving a second business-specific
    join model.  Multiple matches are returned explicitly so source identity
    collisions are never silently merged.
    """
    run = canonical_run_row(connection)
    if run is None:
        raise LookupError("Canonical Semantic Model 尚未完成投影")
    class_iri = "https://semantic.local/ontology/" + object_class_local_name(object_type)
    subjects = connection.execute(
        """SELECT DISTINCT key_stmt.subject_iri
           FROM canonical_statement key_stmt
           JOIN canonical_statement type_stmt
             ON type_stmt.run_id=key_stmt.run_id AND type_stmt.subject_iri=key_stmt.subject_iri
           WHERE key_stmt.run_id=? AND key_stmt.predicate_iri=? AND key_stmt.lexical_value=?
             AND type_stmt.predicate_iri=? AND type_stmt.object_iri=?
           ORDER BY key_stmt.subject_iri""",
        (run["run_id"], _CANONICAL_KEY_PREDICATE, canonical_key, _RDF_TYPE, class_iri),
    ).fetchall()
    if not subjects:
        raise LookupError("Canonical Semantic Model 中不存在该业务对象")
    context_payload = json.loads(SEMANTIC_CONTEXT_FILE.read_text(encoding="utf-8"))
    jsonld_context = context_payload.get("@context", context_payload)

    matches: list[dict[str, Any]] = []
    for subject_row in subjects[:20]:
        subject = str(subject_row["subject_iri"])
        rows = connection.execute(
            """SELECT statement_id,predicate_iri,object_kind,object_iri,lexical_value,
                      datatype_iri,language_tag,confidence,assertion_status
               FROM canonical_statement
               WHERE run_id=? AND subject_iri=?
               ORDER BY predicate_iri,statement_id LIMIT 1000""",
            (run["run_id"], subject),
        ).fetchall()
        properties: dict[str, list[Any]] = {}
        provenance: list[dict[str, Any]] = []
        relation_refs: list[dict[str, Any]] = []
        event_refs: list[dict[str, Any]] = []
        fact_refs: list[dict[str, Any]] = []
        state_refs: list[dict[str, Any]] = []
        types: list[str] = []
        for row in rows:
            predicate = str(row["predicate_iri"])
            if predicate == _RDF_TYPE and row["object_iri"]:
                types.append(str(row["object_iri"]))
                continue
            if row["object_kind"] == "iri":
                value: Any = {"@id": row["object_iri"]}
                ref = {
                    "predicate": predicate.rsplit("/", 1)[-1],
                    "iri": row["object_iri"],
                    "status": row["assertion_status"] or "accepted",
                }
                if predicate == _HAS_EVENT_PREDICATE:
                    event_refs.append(ref)
                elif predicate == _HAS_FACT_PREDICATE:
                    fact_refs.append(ref)
                elif predicate == _HAS_CURRENT_STATE_PREDICATE:
                    state_refs.append(ref)
                elif not predicate.startswith(_PROV_PREFIX):
                    relation_refs.append(ref)
            else:
                value = {"@value": row["lexical_value"]}
                if row["datatype_iri"]:
                    value["@type"] = row["datatype_iri"]
                if row["language_tag"]:
                    value["@language"] = row["language_tag"]
            properties.setdefault(predicate, []).append(value)
            provenance.extend(dict(item) for item in connection.execute(
                """SELECT source_system,source_schema,source_table,source_row_id,
                          source_snapshot_id,activity_type,activity_id,evidence_json
                   FROM canonical_provenance WHERE statement_id=? ORDER BY created_at""",
                (row["statement_id"],),
            ).fetchall())
        jsonld: dict[str, Any] = {"@context": jsonld_context, "@id": subject, "@type": types}
        jsonld.update({key: value[0] if len(value) == 1 else value for key, value in properties.items()})
        matches.append({
            "@id": subject,
            "@type": types,
            "jsonld": jsonld,
            "properties": properties,
            "relations": relation_refs,
            "events": event_refs,
            "facts": fact_refs,
            "currentStates": state_refs,
            "evidence": provenance,
        })
    return {
        "schemaVersion": "semantic-api-v1",
        "runId": run["run_id"],
        "objectType": object_type,
        "canonicalKey": canonical_key,
        "matchCount": len(subjects),
        "ambiguous": len(subjects) > 1,
        "objects": matches,
        "sourceWrite": False,
        "formalPublication": False,
    }


def semantic_object_timeline_payload(
    connection: sqlite3.Connection,
    object_type: str,
    canonical_key: str,
) -> dict[str, Any]:
    """Return a chronological, evidence-bearing event timeline for an object."""
    payload = canonical_object_payload(connection, object_type, canonical_key)
    events: list[dict[str, Any]] = []
    for item in payload["objects"]:
        for reference in item["events"]:
            event_iri = str(reference["iri"])
            rows = connection.execute(
                """SELECT predicate_iri,object_kind,object_iri,lexical_value
                   FROM canonical_statement
                   WHERE run_id=? AND subject_iri=?
                   ORDER BY statement_id""",
                (payload["runId"], event_iri),
            ).fetchall()
            values: dict[str, list[Any]] = {}
            for row in rows:
                predicate = str(row["predicate_iri"])
                value: Any = row["object_iri"] if row["object_kind"] == "iri" else row["lexical_value"]
                values.setdefault(predicate, []).append(value)
            event_types = values.get(_RDF_TYPE, [])
            events.append({
                "iri": event_iri,
                "eventType": [value.rsplit("/", 1)[-1] for value in event_types],
                "occurredAt": values.get(_OCCURRED_AT_PREDICATE, [None])[0],
                "recordedAt": values.get(_RECORDED_AT_PREDICATE, [None])[0],
                "status": values.get(_STATUS_PREDICATE, [None])[0],
                "displayName": values.get(_DISPLAY_NAME_PREDICATE, [None])[0],
                "relation": reference.get("predicate"),
            })
    events.sort(key=lambda item: (str(item.get("occurredAt") or ""), str(item["iri"])))
    return {
        "schemaVersion": "semantic-api-v2",
        "runId": payload["runId"],
        "objectType": payload["objectType"],
        "canonicalKey": payload["canonicalKey"],
        "matchCount": payload["matchCount"],
        "ambiguous": payload["ambiguous"],
        "events": events,
        "sourceWrite": False,
        "formalPublication": False,
    }


def semantic_context_payload(
    canonical: sqlite3.Connection,
    overlay: sqlite3.Connection,
    object_type: str,
    canonical_key: str,
    task: str = "",
    question: str = "",
    time_from: str | None = None,
    time_to: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Build a bounded, evidence-bearing context for an Agent.

    Canonical RDF remains the object/event/fact authority.  The local
    relational layer contributes only released Knowledge, rule indexes and
    approval-gated action references.  This keeps context useful without
    turning the endpoint into a raw database dump.
    """
    object_payload = canonical_object_payload(canonical, object_type, canonical_key)
    timeline = semantic_object_timeline_payload(canonical, object_type, canonical_key)
    if time_from:
        timeline["events"] = [
            item for item in timeline["events"]
            if not item.get("occurredAt") or str(item["occurredAt"]) >= time_from
        ]
    if time_to:
        timeline["events"] = [
            item for item in timeline["events"]
            if not item.get("occurredAt") or str(item["occurredAt"]) <= time_to
        ]
    knowledge_payload = knowledge_for_object_payload(overlay, object_type, canonical_key, limit)
    objects = object_payload["objects"]
    relations = [item for match in objects for item in match["relations"]][:limit]
    facts = [item for match in objects for item in match["facts"]][:limit]
    states = [item for match in objects for item in match["currentStates"]][:limit]
    evidence = [item for match in objects for item in match["evidence"]][: limit * 2]

    rules: list[dict[str, Any]] = []
    if _table_exists(overlay, "semantic_logic_rule"):
        rules.extend(dict(row) for row in overlay.execute(
            "SELECT * FROM semantic_logic_rule WHERE status='enabled' ORDER BY rule_asset_id LIMIT ?",
            (limit,),
        ).fetchall())
    if _table_exists(overlay, "semantic_action_rule"):
        rules.extend(dict(row) for row in overlay.execute(
            """SELECT rule_id AS rule_asset_id,rule_version AS rule_version_id,title AS decision,
                      action_type,risk_level,requires_approval,status,source_write,formal_publication
                 FROM semantic_action_rule WHERE status='enabled' ORDER BY rule_id LIMIT ?""",
            (limit,),
        ).fetchall())

    available_actions: list[dict[str, Any]] = []
    if _table_exists(overlay, "semantic_action_definition"):
        available_actions = [dict(row) for row in overlay.execute(
            "SELECT * FROM semantic_action_definition WHERE status='enabled' ORDER BY action_id LIMIT ?",
            (limit,),
        ).fetchall()]
    return {
        "schemaVersion": "semantic-context-v1",
        "object": object_payload,
        "relations": relations,
        "currentState": states,
        "events": timeline["events"][:limit],
        "facts": facts,
        "rules": rules,
        "knowledge": knowledge_payload["items"],
        "cases": [item for item in knowledge_payload["items"] if item.get("knowledgeKind") in {"case", "evaluation_case"}],
        "evidence": evidence + [source for item in knowledge_payload["items"] for source in item.get("evidence", [])][: limit * 2],
        "availableActions": available_actions,
        "retrieval": {
            "strategy": ["canonical-rdf", "ontology-guided-knowledge", "released-rule-index"],
            "task": task,
            "question": question,
            "timeFrom": time_from,
            "timeTo": time_to,
            "limit": limit,
            "suppressedUnreleasedKnowledge": True,
            "suppressedConflictedKnowledge": True,
        },
        "provenance": {
            "canonicalRunId": object_payload["runId"],
            "objectEvidenceCount": len(evidence),
            "knowledgeEvidenceCount": sum(len(item.get("evidence", [])) for item in knowledge_payload["items"]),
        },
        "policy": {
            "sourceWrite": False,
            "formalPublication": False,
            "agentMayCreateCandidateOnly": True,
            "agentMayPublishKnowledge": False,
            "agentMayExecuteAction": False,
        },
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
