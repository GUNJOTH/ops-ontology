"""Streamed source identity projection helpers for Canonical RDF."""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from build_canonical_semantic_model import EX, graph_iri, literal, short_hash
from rdflib import Literal, URIRef
from rdflib.namespace import XSD


def identity_graph_rows(builder) -> list[tuple[str, str, str]]:
    """Return (snapshot id, graph IRI, graph id) for streamed identity graphs."""
    return [
        (
            snapshot_id,
            graph_iri("source", builder.run_id, snapshot_id),
            f"graph-{short_hash(builder.run_id, graph_iri('source', builder.run_id, snapshot_id))}",
        )
        for snapshot_id in builder.identity_snapshot_ids
    ]

def identity_evidence(builder) -> str:
    return json.dumps({"projection": "source_identity_seed", "semanticRelations": False}, ensure_ascii=False, sort_keys=True)

def stream_identity_projection(
    builder,
    db: sqlite3.Connection,
    identity_db: sqlite3.Connection,
    graph_rows: list[tuple[str, str, str]],
    resources: set[str],
    created_at: str,
) -> dict[str, int]:
    """Write all unified devices in bounded batches directly to SQLite.

    This avoids materialising millions of RDF terms in rdflib.  The same
    rows are emitted to the TriG/JSON-LD artifacts by the artifact writer;
    the SQLite tables remain the local query projection of that dataset.
    """
    stats = {
        "devices": 0,
        "statements": 0,
        "resources": 0,
        "provenanceEligible": 0,
        "provenanceCovered": 0,
    }
    evidence = builder.identity_evidence()
    statement_rows: list[tuple[Any, ...]] = []
    provenance_rows: list[tuple[Any, ...]] = []
    for snapshot_id, graph_iri_value, graph_id in graph_rows:
        cursor = identity_db.execute(
            """
            SELECT unified_device_id,master_source_schema,asset_number,canonical_name,
                   source_identity_key,source_snapshot_id
            FROM unified_device
            WHERE source_snapshot_id=?
            ORDER BY unified_device_id
            """,
            (snapshot_id,),
        )
        while True:
            batch = cursor.fetchmany(2000)
            if not batch:
                break
            for row in batch:
                subject = builder.identity_subject(row["unified_device_id"])
                subject_text = str(subject)
                if subject_text not in resources:
                    resources.add(subject_text)
                    stats["resources"] += 1
                stats["devices"] += 1
                for predicate, obj in builder.identity_statement_specs(row):
                    object_kind = "iri" if isinstance(obj, URIRef) else "literal"
                    object_iri = str(obj) if object_kind == "iri" else None
                    lexical_value = None if object_kind == "iri" else str(obj)
                    datatype_iri = str(obj.datatype) if isinstance(obj, Literal) and obj.datatype else None
                    language_tag = obj.language if isinstance(obj, Literal) else None
                    statement_id = f"stmt-{short_hash(builder.run_id, graph_iri_value, subject, predicate, object_kind, object_iri, lexical_value, datatype_iri, language_tag)}"
                    statement_rows.append((
                        statement_id, builder.run_id, graph_id, subject_text, str(predicate), object_kind,
                        object_iri, lexical_value, datatype_iri, language_tag, None, "seed", created_at,
                    ))
                    provenance_rows.append((
                        f"prov-{short_hash(statement_id, row['source_identity_key'])}", statement_id,
                        row["master_source_schema"], row["master_source_schema"], "identity.unified_device",
                        row["source_identity_key"], row["source_snapshot_id"], "canonical_projection",
                        builder.run_id, evidence, created_at,
                    ))
                    stats["statements"] += 1
                    stats["provenanceEligible"] += 1
                    stats["provenanceCovered"] += 1
                if stats["devices"] % 100000 == 0:
                    print(f"[canonical] streamed identity devices: {stats['devices']}", flush=True)
            db.executemany(
                """
                INSERT OR IGNORE INTO canonical_statement(
                  statement_id,run_id,graph_id,subject_iri,predicate_iri,object_kind,
                  object_iri,lexical_value,datatype_iri,language_tag,confidence,
                  assertion_status,created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                statement_rows,
            )
            db.executemany(
                """
                INSERT OR IGNORE INTO canonical_provenance(
                  provenance_id,statement_id,source_system,source_schema,source_table,
                  source_row_id,source_snapshot_id,activity_type,activity_id,evidence_json,created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """,
                provenance_rows,
            )
            statement_rows.clear()
            provenance_rows.clear()
    return stats

def write_identity_trig(
    builder,
    output: Any,
    identity_db: sqlite3.Connection,
    graph_rows: list[tuple[str, str, str]],
) -> None:
    written = 0
    for snapshot_id, graph_iri_value, _graph_id in graph_rows:
        output.write(f"<{graph_iri_value}> {{\n")
        cursor = identity_db.execute(
            """
            SELECT unified_device_id,master_source_schema,asset_number,canonical_name,
                   source_identity_key,source_snapshot_id
            FROM unified_device
            WHERE source_snapshot_id=?
            ORDER BY unified_device_id
            """,
            (snapshot_id,),
        )
        while True:
            batch = cursor.fetchmany(2000)
            if not batch:
                break
            for row in batch:
                written += 1
                subject = builder.identity_subject(row["unified_device_id"]).n3()
                values = [
                    ("a", URIRef(EX + "Device").n3()),
                    (URIRef(EX + "canonicalKey").n3(), literal(row["unified_device_id"], XSD.string).n3()),
                    (URIRef(EX + "displayName").n3(), literal(builder.identity_display_name(row), XSD.string).n3()),
                    (URIRef(EX + "sourceNamespace").n3(), literal(row["master_source_schema"], XSD.string).n3()),
                    (URIRef(EX + "sourceSnapshotId").n3(), literal(row["source_snapshot_id"], XSD.string).n3()),
                    (URIRef(EX + "sourceRecordId").n3(), literal(row["source_identity_key"], XSD.string).n3()),
                ]
                output.write(f"    {subject} {values[0][0]} {values[0][1]} ;\n")
                for index, (predicate, value) in enumerate(values[1:], start=1):
                    suffix = " ;" if index < len(values) - 1 else " ."
                    output.write(f"        {predicate} {value}{suffix}\n")
                if written % 100000 == 0:
                    print(f"[canonical] wrote TriG identity devices: {written}", flush=True)
        output.write("}\n\n")

def identity_json_node(builder, row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": str(builder.identity_subject(row["unified_device_id"])),
        "type": "Device",
        "canonicalKey": str(row["unified_device_id"]),
        "displayName": builder.identity_display_name(row),
        "sourceNamespace": str(row["master_source_schema"]),
        "sourceSnapshotId": str(row["source_snapshot_id"]),
        "sourceRecordId": str(row["source_identity_key"]),
    }

def write_identity_jsonld(
    builder,
    output: Any,
    identity_db: sqlite3.Connection,
    graph_rows: list[tuple[str, str, str]],
) -> None:
    written = 0
    first_graph = True
    for snapshot_id, graph_iri_value, _graph_id in graph_rows:
        if not first_graph:
            output.write(",")
        first_graph = False
        # The wrapper is written explicitly so every identity node remains
        # a member of one JSON-LD named graph without holding the graph in
        # memory.
        output.write(json.dumps({"id": graph_iri_value}, ensure_ascii=False)[:-1] + ',"@graph":[')
        first_node = True
        cursor = identity_db.execute(
            """
            SELECT unified_device_id,master_source_schema,asset_number,canonical_name,
                   source_identity_key,source_snapshot_id
            FROM unified_device
            WHERE source_snapshot_id=?
            ORDER BY unified_device_id
            """,
            (snapshot_id,),
        )
        while True:
            batch = cursor.fetchmany(2000)
            if not batch:
                break
            for row in batch:
                written += 1
                if not first_node:
                    output.write(",")
                first_node = False
                output.write(json.dumps(builder.identity_json_node(row), ensure_ascii=False, separators=(",", ":")))
                if written % 100000 == 0:
                    print(f"[canonical] wrote JSON-LD identity devices: {written}", flush=True)
        output.write("]}")
