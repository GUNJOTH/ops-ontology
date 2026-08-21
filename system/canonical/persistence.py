"""Persistence stage for the Canonical RDF projection.

This module owns artifact materialization and local projection writes.  RDF
mapping and graph construction remain on ``CanonicalBuilder``; keeping the
write boundary here makes release-side behavior independently testable.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path
from typing import Any, Callable

from rdflib import Literal, URIRef


def persist_canonical_dataset(
    builder: Any,
    db: sqlite3.Connection,
    issues: list[dict[str, str]],
    identity_db: sqlite3.Connection | None = None,
    *,
    run_root: Path,
    standard_root: Path,
    manifest_path_fn: Callable[[Path, Path], str],
    load_jsonld_context_fn: Callable[[], dict[str, Any]],
    short_hash_fn: Callable[..., str],
    json_text_fn: Callable[[object], str],
) -> dict[str, Any]:
    """Materialize TriG/JSON-LD and persist the local canonical projection."""
    run_dir = run_root / builder.run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    trig_path = run_dir / "canonical.trig"
    jsonld_path = run_dir / "canonical.jsonld"
    reasoning_trig_path = run_dir / "canonical.reasoning.trig"
    reasoning_jsonld_path = run_dir / "canonical.reasoning.jsonld"
    base_trig_path = run_dir / "canonical.base.trig"
    base_jsonld_path = run_dir / "canonical.base.jsonld"
    manifest_file = run_dir / "projection_manifest.json"
    validation_path = run_dir / "shacl_validation_report.json"

    # The bounded semantic graph is also the reasoning input. Full source
    # identity rows are appended as a streamed graph below.
    builder.dataset.serialize(destination=str(base_trig_path), format="trig")
    builder.dataset.serialize(
        destination=str(base_jsonld_path),
        format="json-ld",
        context=load_jsonld_context_fn(),
        auto_compact=True,
    )
    shutil.copyfile(base_trig_path, reasoning_trig_path)
    shutil.copyfile(base_jsonld_path, reasoning_jsonld_path)

    graph_map = {str(context.identifier): context for context in builder.dataset.contexts()}
    graph_rows: dict[str, str] = {}
    graph_kinds: dict[str, str] = {}
    for graph_iri_value in graph_map:
        kind = (
            "ontology"
            if "/ontology/" in graph_iri_value
            else "source"
            if "/source/" in graph_iri_value
            else "derived"
            if "/derived/" in graph_iri_value
            else "provenance"
        )
        graph_id = f"graph-{short_hash_fn(builder.run_id, graph_iri_value)}"
        graph_rows[graph_iri_value] = graph_id
        graph_kinds[graph_iri_value] = kind
        snapshot_id = graph_iri_value.rsplit("/", 1)[-1] if kind == "source" else None
        db.execute(
            "INSERT INTO canonical_graph(graph_id,run_id,graph_iri,graph_kind,source_snapshot_id,status,created_at) VALUES (?,?,?,?,?,?,?)",
            (graph_id, builder.run_id, graph_iri_value, kind, snapshot_id, "active", builder.created_at),
        )

    identity_graph_rows = (
        builder._identity_graph_rows()
        if identity_db is not None and builder.identity_preflight.get("status") == "ready"
        else []
    )
    for snapshot_id, graph_iri_value, graph_id in identity_graph_rows:
        graph_rows[graph_iri_value] = graph_id
        graph_kinds[graph_iri_value] = "source"
        db.execute(
            "INSERT INTO canonical_graph(graph_id,run_id,graph_iri,graph_kind,source_snapshot_id,status,created_at) VALUES (?,?,?,?,?,?,?)",
            (graph_id, builder.run_id, graph_iri_value, "source", snapshot_id, "active", builder.created_at),
        )

    statements = 0
    resources: set[str] = set()
    provenance_eligible = 0
    provenance_covered = 0
    provenance_by_graph: dict[str, dict[str, int]] = {}
    for context in builder.dataset.contexts():
        graph_iri_value = str(context.identifier)
        graph_id = graph_rows[graph_iri_value]
        graph_kind = graph_kinds[graph_iri_value]
        graph_metrics = provenance_by_graph.setdefault(graph_kind, {"eligible": 0, "covered": 0})
        for subject, predicate, obj in context:
            object_kind = "iri" if isinstance(obj, URIRef) else "literal"
            object_iri = str(obj) if object_kind == "iri" else None
            lexical_value = None if object_kind == "iri" else str(obj)
            datatype_iri = str(obj.datatype) if isinstance(obj, Literal) and obj.datatype else None
            language_tag = obj.language if isinstance(obj, Literal) else None
            sid = f"stmt-{short_hash_fn(builder.run_id, graph_iri_value, subject, predicate, object_kind, object_iri, lexical_value, datatype_iri, language_tag)}"
            context_info = builder.statement_context.get((subject, predicate, obj), {})
            statement_provenance = context_info.get("provenance") or builder.subject_provenance.get(subject)
            if graph_kind in {"source", "derived"}:
                provenance_eligible += 1
                graph_metrics["eligible"] += 1
                if statement_provenance:
                    provenance_covered += 1
                    graph_metrics["covered"] += 1
            db.execute(
                "INSERT OR IGNORE INTO canonical_statement(statement_id,run_id,graph_id,subject_iri,predicate_iri,object_kind,object_iri,lexical_value,datatype_iri,language_tag,confidence,assertion_status,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (sid, builder.run_id, graph_id, str(subject), str(predicate), object_kind, object_iri, lexical_value, datatype_iri, language_tag, context_info.get("confidence"), context_info.get("status", "accepted"), builder.created_at),
            )
            provenance = statement_provenance or {}
            if provenance:
                db.execute(
                    "INSERT OR IGNORE INTO canonical_provenance(provenance_id,statement_id,source_system,source_schema,source_table,source_row_id,source_snapshot_id,activity_type,activity_id,evidence_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (f"prov-{short_hash_fn(sid, provenance.get('source_row_id'), provenance.get('event_id'), provenance.get('fact_id'))}", sid, provenance.get("source_system"), provenance.get("source_schema"), provenance.get("source_table"), provenance.get("source_row_id"), provenance.get("source_snapshot_id"), "canonical_projection", builder.run_id, json_text_fn(provenance.get("evidence_json") or provenance), builder.created_at),
                )
            statements += 1
            resources.add(str(subject))
            if object_kind == "iri":
                resources.add(str(obj))

    identity_stats = {"devices": 0, "statements": 0, "resources": 0, "provenanceEligible": 0, "provenanceCovered": 0}
    if identity_db is not None and identity_graph_rows:
        identity_stats = builder._stream_identity_projection(db, identity_db, identity_graph_rows, resources, builder.created_at)
        statements += identity_stats["statements"]
        provenance_eligible += identity_stats["provenanceEligible"]
        provenance_covered += identity_stats["provenanceCovered"]
        source_metrics = provenance_by_graph.setdefault("source", {"eligible": 0, "covered": 0})
        source_metrics["eligible"] += identity_stats["provenanceEligible"]
        source_metrics["covered"] += identity_stats["provenanceCovered"]

    base_jsonld = json.loads(base_jsonld_path.read_text(encoding="utf-8"))
    base_context = base_jsonld.get("@context", load_jsonld_context_fn())
    base_graphs = base_jsonld.get("@graph", [])
    with trig_path.open("w", encoding="utf-8", newline="\n") as output:
        output.write(base_trig_path.read_text(encoding="utf-8"))
        if identity_db is not None and identity_graph_rows:
            builder._write_identity_trig(output, identity_db, identity_graph_rows)
    with jsonld_path.open("w", encoding="utf-8", newline="\n") as output:
        output.write('{"@context":')
        output.write(json.dumps(base_context, ensure_ascii=False, separators=(",", ":")))
        output.write(',"@graph":[')
        first = True
        for item in base_graphs:
            if not first:
                output.write(",")
            first = False
            output.write(json.dumps(item, ensure_ascii=False, separators=(",", ":")))
        if identity_db is not None and identity_graph_rows:
            if not first:
                output.write(",")
            builder._write_identity_jsonld(output, identity_db, identity_graph_rows)
        output.write("]}")
    base_trig_path.unlink(missing_ok=True)
    base_jsonld_path.unlink(missing_ok=True)

    manifest = {
        "schemaVersion": "canonical-semantic-model-v1",
        "runId": builder.run_id,
        "sourceDb": manifest_path_fn(builder.source, run_dir),
        "sourceSnapshotId": builder.source_snapshot_id,
        "ontologyVersion": builder.ontology_version,
        "standardBaseline": ["RDF 1.1", "RDFS 1.1", "OWL 2 RL", "SKOS", "SHACL 1.0", "SPARQL 1.1", "PROV-O", "JSON-LD 1.1"],
        "graphCount": len(graph_rows),
        "resourceCount": len(resources),
        "statementCount": statements,
        "validationErrorCount": len(issues),
        "sourceWrite": False,
        "formalPublication": False,
        "vocabulary": builder.vocabulary_counts,
        "canonicalScope": {
            "semanticOverlayObjects": "semantic_object_instance",
            "sourceIdentityGraph": "all unified_device rows from the latest read-only identity snapshot",
            "sourceIdentityDeviceCount": int(identity_stats["devices"]),
            "sourceIdentitySnapshots": builder.identity_snapshot_ids,
            "relationsEventsFactsStates": "only evidence-backed semantic overlay rows; source identity projection does not infer them",
        },
        "streamingProjection": bool(identity_graph_rows),
        "provenanceCoverage": {
            "eligibleStatements": provenance_eligible,
            "coveredStatements": provenance_covered,
            "rate": round(provenance_covered / provenance_eligible, 6) if provenance_eligible else 1.0,
            "byGraph": provenance_by_graph,
        },
        "artifacts": {
            "trig": manifest_path_fn(trig_path, run_dir),
            "jsonld": manifest_path_fn(jsonld_path, run_dir),
            "reasoningTrig": manifest_path_fn(reasoning_trig_path, run_dir),
            "reasoningJsonLd": manifest_path_fn(reasoning_jsonld_path, run_dir),
            "validation": manifest_path_fn(validation_path, run_dir),
            "rdfDatasetManifest": manifest_path_fn(standard_root / str(builder.version_spec.get("rdfDatasetManifest", "rdf-dataset.json")), run_dir),
            "assetManifest": manifest_path_fn(standard_root / str(builder.version_spec.get("assetManifest", "semantic-asset-manifest.json")), run_dir),
        },
    }
    manifest_file.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    validation_path.write_text(json.dumps({"conforms": not issues, "issues": issues, "identityPreflight": builder.identity_preflight}, ensure_ascii=False, indent=2), encoding="utf-8")
    db.execute(
        "INSERT INTO canonical_projection_run(run_id,semantic_source_db,source_snapshot_id,ontology_version,graph_count,resource_count,statement_count,validation_error_count,status,manifest_json,source_write,formal_publication,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (builder.run_id, str(builder.source), builder.source_snapshot_id, builder.ontology_version, len(graph_rows), len(resources), statements, len(issues), "completed" if not issues else "completed_with_errors", json_text_fn(manifest), 0, 0, builder.created_at),
    )
    db.commit()
    return manifest
