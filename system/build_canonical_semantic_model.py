"""Build the canonical RDF semantic model from the local semantic runtime.

The source systems and the existing ``semantic_*`` tables are read-only inputs
to this builder.  The canonical model is a versioned RDF Dataset persisted in
a local SQLite projection for efficient API access, plus TriG and JSON-LD
artifacts for standards interoperability.

This is deliberately a projection boundary, not another set of business
tables: ``semantic_*`` remains mapping/governance/runtime data, while the
canonical RDF Dataset is the only standard semantic representation.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sqlite3
from typing import Any, Iterable
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

try:
    from rdflib import BNode, Dataset, Graph, Literal, Namespace, URIRef
    from rdflib.namespace import OWL, RDF, RDFS, XSD
except ImportError as exc:  # pragma: no cover - gives an actionable runtime error
    raise SystemExit(
        "缺少 rdflib，请安装 system/requirements.txt 后重试：pip install -r system/requirements.txt"
    ) from exc

from semantic_predicates import event_predicate, relation_predicate
from semantic_registry import object_class_local_name
from semantic_namespaces import GRAPH_NAMESPACE, ONTOLOGY_NAMESPACE, RESOURCE_NAMESPACE, SOURCE_NAMESPACE


ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parent
DEFAULT_SOURCE = ROOT / "data" / "unified_semantics.sqlite3"
DEFAULT_TARGET = ROOT / "data" / "canonical_semantic.sqlite3"
IDENTITY_ROOT = PROJECT_ROOT / "pilots" / "identity" / "results"
STANDARD_ROOT = PROJECT_ROOT / "standards"
RUN_ROOT = ROOT / "canonical-runs"
VOCABULARY_PATH = STANDARD_ROOT / "vocabularies.ttl"
VERSION_SPEC_PATH = STANDARD_ROOT / "ontology-version.json"

EX = Namespace(ONTOLOGY_NAMESPACE)
PROV = Namespace("http://www.w3.org/ns/prov#")
SKOS = Namespace("http://www.w3.org/2004/02/skos/core#")
SOURCE_BASE = SOURCE_NAMESPACE
GRAPH_BASE = GRAPH_NAMESPACE
RESOURCE_BASE = RESOURCE_NAMESPACE

VOCABULARY_SCHEME_BY_TYPE = {
    "device": EX.DeviceClassificationScheme,
    "location": EX.DeviceClassificationScheme,
    "inspection": EX.DeviceClassificationScheme,
    "defect": EX.DeviceClassificationScheme,
    "work_order": EX.DeviceClassificationScheme,
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def short_hash(*parts: object) -> str:
    raw = "|".join("" if part is None else str(part) for part in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def safe_segment(value: object) -> str:
    return quote(str(value or "unknown"), safe="-._~")


def source_iri(namespace: object, kind: str, key: object) -> URIRef:
    return URIRef(f"{SOURCE_BASE}{safe_segment(namespace)}/{kind}/{safe_segment(key)}")


def class_iri(object_type: object) -> URIRef:
    key = str(object_type or "business_object").strip().lower()
    return URIRef(EX + object_class_local_name(key))


def table_exists(db: sqlite3.Connection, name: str) -> bool:
    return db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone() is not None


def latest_identity_source() -> Path | None:
    """Return the newest local identity result without touching source systems."""
    for directory in sorted(IDENTITY_ROOT.glob("identity-layer-v1-*/"), reverse=True):
        database = directory / "identity_semantics.sqlite3"
        if (directory / "manifest.json").exists() and database.exists():
            return database
    return None


def literal(value: object, datatype: URIRef | None = None) -> Literal:
    if value is None:
        return Literal("")
    return Literal(str(value), datatype=datatype) if datatype else Literal(str(value))


def datetime_literal(value: object) -> Literal | None:
    if not value:
        return None
    text = str(value).strip().replace(" ", "T", 1)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", text):
        text += "Z"
    try:
        datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return literal(value)
    return Literal(text, datatype=XSD.dateTime)


def json_text(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def ensure_schema(db: sqlite3.Connection) -> None:
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS canonical_projection_run (
          run_id TEXT PRIMARY KEY,
          semantic_source_db TEXT NOT NULL,
          source_snapshot_id TEXT,
          ontology_version TEXT NOT NULL,
          graph_count INTEGER NOT NULL,
          resource_count INTEGER NOT NULL,
          statement_count INTEGER NOT NULL,
          validation_error_count INTEGER NOT NULL,
          status TEXT NOT NULL CHECK(status IN ('completed','completed_with_errors','blocked')),
          manifest_json TEXT NOT NULL,
          source_write INTEGER NOT NULL CHECK(source_write=0),
          formal_publication INTEGER NOT NULL CHECK(formal_publication=0),
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS canonical_graph (
          graph_id TEXT PRIMARY KEY,
          run_id TEXT NOT NULL,
          graph_iri TEXT NOT NULL,
          graph_kind TEXT NOT NULL,
          source_snapshot_id TEXT,
          status TEXT NOT NULL,
          created_at TEXT NOT NULL,
          UNIQUE(run_id, graph_iri)
        );
        CREATE INDEX IF NOT EXISTS ix_canonical_graph_latest
          ON canonical_graph(run_id,graph_kind,graph_iri);
        CREATE TABLE IF NOT EXISTS canonical_statement (
          statement_id TEXT PRIMARY KEY,
          run_id TEXT NOT NULL,
          graph_id TEXT NOT NULL,
          subject_iri TEXT NOT NULL,
          predicate_iri TEXT NOT NULL,
          object_kind TEXT NOT NULL CHECK(object_kind IN ('iri','literal')),
          object_iri TEXT,
          lexical_value TEXT,
          datatype_iri TEXT,
          language_tag TEXT,
          confidence REAL,
          assertion_status TEXT NOT NULL,
          created_at TEXT NOT NULL,
          UNIQUE(run_id,graph_id,subject_iri,predicate_iri,object_kind,object_iri,lexical_value,datatype_iri,language_tag)
        );
        CREATE INDEX IF NOT EXISTS ix_canonical_statement_subject
          ON canonical_statement(run_id,subject_iri,predicate_iri);
        CREATE INDEX IF NOT EXISTS ix_canonical_statement_object
          ON canonical_statement(run_id,object_iri);
        CREATE TABLE IF NOT EXISTS canonical_provenance (
          provenance_id TEXT PRIMARY KEY,
          statement_id TEXT NOT NULL,
          source_system TEXT,
          source_schema TEXT,
          source_table TEXT,
          source_row_id TEXT,
          source_snapshot_id TEXT,
          activity_type TEXT NOT NULL,
          activity_id TEXT NOT NULL,
          evidence_json TEXT NOT NULL,
          created_at TEXT NOT NULL,
          UNIQUE(statement_id,activity_id,source_row_id)
        );
        CREATE INDEX IF NOT EXISTS ix_canonical_provenance_statement
          ON canonical_provenance(statement_id,created_at);
        """
    )


def load_version_spec() -> dict[str, Any]:
    return json.loads(VERSION_SPEC_PATH.read_text(encoding="utf-8"))


def graph_iri(kind: str, run_id: str, snapshot_id: str | None = None, ontology_version: str | None = None) -> str:
    if kind == "ontology":
        return f"{GRAPH_BASE}ontology/{quote(str(ontology_version or 'enterprise-operations-ontology/v1'), safe='-._~/')}"
    if kind == "source":
        return f"{GRAPH_BASE}source/{safe_segment(snapshot_id or 'unknown')}"
    if kind == "derived":
        return f"{GRAPH_BASE}derived/{safe_segment(run_id)}"
    return f"{GRAPH_BASE}{kind}/{safe_segment(run_id)}"


def resource_iri(object_type: object, key: object, object_index: dict[tuple[str, str], URIRef]) -> URIRef:
    type_key = str(object_type or "business_object").strip().lower()
    key_text = str(key or "unknown")
    return object_index.get((type_key, key_text), URIRef(RESOURCE_BASE + f"{safe_segment(type_key)}/{safe_segment(key_text)}"))


def add_literal_properties(dataset: Dataset, graph: Graph, subject: URIRef, values: Iterable[tuple[URIRef, object, URIRef | None]]) -> None:
    for predicate, value, datatype in values:
        if value is None or value == "":
            continue
        graph.add((subject, predicate, value if isinstance(value, Literal) else literal(value, datatype)))


def load_ontology() -> Graph:
    ontology = Graph()
    spec = load_version_spec()
    ontology.parse(str(STANDARD_ROOT / str(spec.get("ontologyFile", "ontology.ttl"))), format="turtle")
    return ontology


def load_vocabulary() -> Graph:
    vocabulary = Graph()
    spec = load_version_spec()
    vocabulary.parse(str(STANDARD_ROOT / str(spec.get("vocabularyFile", VOCABULARY_PATH.name))), format="turtle")
    return vocabulary


def load_jsonld_context() -> dict[str, Any]:
    spec = load_version_spec()
    payload = json.loads((STANDARD_ROOT / str(spec.get("contextFile", "context.jsonld"))).read_text(encoding="utf-8"))
    context = payload.get("@context", payload)
    if not isinstance(context, dict):
        raise ValueError("JSON-LD context must be an object")
    return context


def validate_shapes(dataset: Dataset) -> list[dict[str, str]]:
    """Validate the SHACL Core subset declared by the canonical shapes.

    The implementation is intentionally deterministic and local. It supports
    targetClass, path, min/maxCount, datatype, nodeKind, class, in, severity,
    message and closed/ignoredProperties. A full pySHACL run can be added as a
    second implementation without changing the asset contract.
    """
    shapes = Graph()
    spec = load_version_spec()
    shapes.parse(str(STANDARD_ROOT / str(spec.get("shaclFile", "enterprise-operations.shacl.ttl"))), format="turtle")
    data = Graph()
    for subject, predicate, obj, _graph in dataset.quads((None, None, None, None)):
        data.add((subject, predicate, obj))
    issues: list[dict[str, str]] = []

    sh = Namespace("http://www.w3.org/ns/shacl#")

    def list_items(head: object) -> list[object]:
        values: list[object] = []
        current = head
        while current and current != RDF.nil:
            value = shapes.value(current, RDF.first)
            if value is None:
                break
            values.append(value)
            current = shapes.value(current, RDF.rest)
        return values

    def class_compatible(value: object, expected: object) -> bool:
        if not isinstance(value, (URIRef, BNode)):
            return False
        pending = list(data.objects(value, RDF.type))
        seen: set[object] = set()
        while pending:
            current = pending.pop()
            if current in seen:
                continue
            seen.add(current)
            if current == expected:
                return True
            pending.extend(data.objects(current, RDFS.subClassOf))
        return False

    def add_issue(node: object, path: object, code: str, shape: object, property_shape: object, fallback: str) -> None:
        severity = shapes.value(property_shape, sh.severity) or shapes.value(shape, sh.severity) or sh.Violation
        message = shapes.value(property_shape, sh.message) or shapes.value(shape, sh.message)
        issues.append({
            "focusNode": str(node),
            "path": str(path),
            "code": code,
            "severity": str(severity).rsplit("#", 1)[-1],
            "message": str(message) if message is not None else fallback,
        })

    def in_allowed_values(value: object, allowed_values: list[object]) -> bool:
        return any(value == allowed or str(value) == str(allowed) for allowed in allowed_values)

    for shape in shapes.subjects(RDF.type, sh.NodeShape):
        target_class = shapes.value(shape, sh.targetClass)
        if target_class is None:
            continue
        focus_nodes = set(data.subjects(RDF.type, target_class))
        property_shapes = list(shapes.objects(shape, sh.property))
        allowed_paths = {path for path in (shapes.value(item, sh.path) for item in property_shapes) if path is not None}
        if str(shapes.value(shape, sh.closed)).lower() == str(True).lower():
            ignored = set(list_items(shapes.value(shape, sh.ignoredProperties)))
            for node in focus_nodes:
                for predicate in data.predicates(node):
                    if predicate not in allowed_paths and predicate not in ignored:
                        add_issue(node, predicate, "closed", shape, shape, "属性不在闭合形状允许列表内")
        for property_shape in property_shapes:
            path = shapes.value(property_shape, sh.path)
            if path is None:
                continue
            min_count = int(shapes.value(property_shape, sh.minCount) or 0)
            max_term = shapes.value(property_shape, sh.maxCount)
            max_count = int(max_term) if max_term is not None else None
            datatype = shapes.value(property_shape, sh.datatype)
            node_kind = shapes.value(property_shape, sh.nodeKind)
            expected_class = shapes.value(property_shape, sh["class"])
            allowed_values = list_items(shapes.value(property_shape, sh["in"]))
            for node in focus_nodes:
                values = list(data.objects(node, path))
                if len(values) < min_count:
                    add_issue(node, path, "minCount", shape, property_shape, f"需要至少 {min_count} 个值")
                if max_count is not None and len(values) > max_count:
                    add_issue(node, path, "maxCount", shape, property_shape, f"最多允许 {max_count} 个值")
                if allowed_values:
                    for value in values:
                        if not in_allowed_values(value, allowed_values):
                            add_issue(node, path, "in", shape, property_shape, "值不在 sh:in 允许列表内")
                if node_kind is not None:
                    for value in values:
                        valid = (
                            node_kind == sh.IRI and isinstance(value, URIRef)
                        ) or (
                            node_kind == sh.Literal and isinstance(value, Literal)
                        ) or (
                            node_kind == sh.BlankNode and isinstance(value, BNode)
                        )
                        if not valid:
                            add_issue(node, path, "nodeKind", shape, property_shape, f"值不满足 {node_kind} 节点类型")
                if expected_class is not None:
                    for value in values:
                        if not class_compatible(value, expected_class):
                            add_issue(node, path, "class", shape, property_shape, f"值不满足 {expected_class} 类型")
                if datatype is not None:
                    for value in values:
                        if not isinstance(value, Literal):
                            add_issue(node, path, "datatype", shape, property_shape, "值必须是 literal")
                        elif datatype == XSD.string and value.datatype not in (None, XSD.string):
                            add_issue(node, path, "datatype", shape, property_shape, "值必须是 xsd:string")
                        elif datatype != XSD.string and value.datatype != datatype:
                            add_issue(node, path, "datatype", shape, property_shape, f"值必须是 {datatype}")
    return issues


class CanonicalBuilder:
    def __init__(self, source: Path, target: Path, identity_source: Path | None = None) -> None:
        self.source = source
        self.target = target
        self.identity_source = identity_source
        self.created_at = utc_now()
        self.version_spec = load_version_spec()
        self.ontology_version = str(self.version_spec["version"])
        self.run_id = f"canonical-projection-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{short_hash(self.created_at)[:10]}"
        self.dataset = Dataset()
        self.source_graphs: dict[str, Graph] = {}
        self.derived_graph = self.dataset.graph(URIRef(graph_iri("derived", self.run_id)))
        self.provenance_graph = self.dataset.graph(URIRef(graph_iri("provenance", self.run_id)))
        self.object_index: dict[tuple[str, str], URIRef] = {}
        self.statement_context: dict[tuple[URIRef, URIRef, URIRef], dict[str, Any]] = {}
        self.subject_provenance: dict[URIRef, dict[str, Any]] = {}
        self.source_snapshot_id: str | None = None
        self.identity_snapshot_ids: list[str] = []
        self.identity_preflight: dict[str, Any] = {
            "enabled": False,
            "database": str(identity_source) if identity_source else None,
            "deviceCount": 0,
            "snapshotIds": [],
            "blankRequiredFields": {},
            "status": "not_configured",
        }
        self.vocabulary_counts: dict[str, int] = {
            "staticStatements": 0,
            "staticDeviceClasses": 0,
            "staticStates": 0,
            "staticUnits": 0,
            "staticTerms": 0,
            "concepts": 0,
            "states": 0,
            "statuses": 0,
            "units": 0,
            "terms": 0,
        }

    def prepare_identity_projection(self, identity_db: sqlite3.Connection | None) -> None:
        """Preflight the full identity population before any local write.

        The identity database is a local, read-only result of the identity
        pipeline.  This method only checks the stable source identity fields;
        it deliberately does not infer relations, events, locations or
        cross-system merges.
        """
        if identity_db is None:
            return
        table = identity_db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='unified_device'"
        ).fetchone()
        if table is None:
            self.identity_preflight.update({"enabled": True, "status": "missing_table"})
            return
        required = {
            "unifiedDeviceId": "unified_device_id",
            "sourceNamespace": "master_source_schema",
            "sourceSnapshotId": "source_snapshot_id",
            "sourceIdentityKey": "source_identity_key",
        }
        blanks: dict[str, int] = {}
        for label, column in required.items():
            blanks[label] = int(identity_db.execute(
                f"SELECT count(*) FROM unified_device WHERE {column} IS NULL OR trim({column})=''"
            ).fetchone()[0])
        blanks["displayNameAfterFallback"] = int(identity_db.execute(
            """
            SELECT count(*) FROM unified_device
            WHERE trim(coalesce(nullif(trim(canonical_name),''),
                                 nullif(trim(asset_number),''),
                                 unified_device_id,''))=''
            """
        ).fetchone()[0])
        device_count = int(identity_db.execute("SELECT count(*) FROM unified_device").fetchone()[0])
        snapshot_rows = identity_db.execute(
            """
            SELECT source_snapshot_id,count(*) AS row_count
            FROM unified_device
            GROUP BY source_snapshot_id
            ORDER BY source_snapshot_id
            """
        ).fetchall()
        self.identity_snapshot_ids = [str(row[0]) for row in snapshot_rows if row[0]]
        self.identity_preflight = {
            "enabled": True,
            "database": str(self.identity_source) if self.identity_source else None,
            "deviceCount": device_count,
            "snapshotIds": self.identity_snapshot_ids,
            "snapshotCounts": {str(row[0]): int(row[1]) for row in snapshot_rows if row[0]},
            "blankRequiredFields": blanks,
            "status": "ready" if not any(blanks.values()) else "blocked",
        }

    @staticmethod
    def identity_subject(unified_device_id: object) -> URIRef:
        # Use the same stable resource IRI as the semantic overlay so the
        # canonical reader can switch authority without changing API IDs.
        return URIRef(EX + f"object/device/{safe_segment(unified_device_id)}")

    @staticmethod
    def identity_display_name(row: sqlite3.Row) -> str:
        return str(
            row["canonical_name"]
            or row["asset_number"]
            or row["unified_device_id"]
            or "未命名设备"
        )

    def identity_statement_specs(self, row: sqlite3.Row) -> list[tuple[URIRef, Literal]]:
        """The minimal, fully-provenanced source identity projection.

        Site, location, classification and parent-child values remain in the
        identity result database until their own evidence-backed mappings are
        projected.  This prevents a full identity projection from silently
        creating business relations.
        """
        return [
            (RDF.type, URIRef(EX + "Device")),
            (EX.canonicalKey, literal(row["unified_device_id"], XSD.string)),
            (EX.displayName, literal(self.identity_display_name(row), XSD.string)),
            (EX.sourceNamespace, literal(row["master_source_schema"], XSD.string)),
            (EX.sourceSnapshotId, literal(row["source_snapshot_id"], XSD.string)),
            (EX.sourceRecordId, literal(row["source_identity_key"], XSD.string)),
        ]

    def source_graph(self, snapshot_id: str | None) -> Graph:
        key = str(snapshot_id or self.source_snapshot_id or "unknown")
        if key not in self.source_graphs:
            self.source_graphs[key] = self.dataset.graph(URIRef(graph_iri("source", self.run_id, key)))
        return self.source_graphs[key]

    def add(self, graph: Graph, subject: URIRef, predicate: URIRef, obj: URIRef | Literal, *, confidence: float | None = None, status: str = "accepted", provenance: dict[str, Any] | None = None) -> None:
        graph.add((subject, predicate, obj))
        self.statement_context[(subject, predicate, obj)] = {"confidence": confidence, "status": status, "provenance": provenance}

    def add_provenance(self, activity_type: str, activity_id: str, subject: URIRef, *, source_system: object = None, source_schema: object = None, source_table: object = None, source_row_id: object = None, source_snapshot_id: object = None, evidence: object = None) -> None:
        provenance = {
            "source_system": source_system,
            "source_schema": source_schema,
            "source_table": source_table,
            "source_row_id": source_row_id,
            "source_snapshot_id": source_snapshot_id,
            "activity_type": activity_type,
            "activity_id": activity_id,
            "evidence_json": evidence,
        }
        self.subject_provenance[subject] = provenance
        activity = URIRef(EX + f"activity/{safe_segment(activity_id)}")
        self.add(self.provenance_graph, activity, RDF.type, PROV.Activity)
        self.add(self.provenance_graph, activity, EX.activityType, literal(activity_type))
        self.add(self.provenance_graph, subject, PROV.wasDerivedFrom, source_iri(source_system or source_schema or "local", "record", source_row_id or activity_id))
        self.add(self.provenance_graph, subject, PROV.wasGeneratedBy, activity)
        self.add(self.provenance_graph, activity, EX.sourceSnapshotId, literal(source_snapshot_id or "local"))
        if evidence is not None:
            self.add(self.provenance_graph, activity, EX.evidenceJson, literal(json_text(evidence)))

    def build_ontology(self, source_db: sqlite3.Connection) -> None:
        graph = self.dataset.graph(URIRef(graph_iri("ontology", self.run_id, ontology_version=self.ontology_version)))
        for triple in load_ontology():
            graph.add(triple)

        static_vocabulary = load_vocabulary()
        for triple in static_vocabulary:
            graph.add(triple)
            self.vocabulary_counts["staticStatements"] += 1
        static_scheme_counts = {
            EX.DeviceClassificationScheme: "staticDeviceClasses",
            EX.DefectStateScheme: "staticStates",
            EX.UnitScheme: "staticUnits",
            EX.TerminologyScheme: "staticTerms",
        }
        for concept in static_vocabulary.subjects(RDF.type, SKOS.Concept):
            scheme = static_vocabulary.value(concept, SKOS.inScheme)
            bucket = static_scheme_counts.get(scheme)
            if bucket:
                self.vocabulary_counts[bucket] += 1

        def table_exists(name: str) -> bool:
            return source_db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
            ).fetchone() is not None

        def add_concept(
            concept: URIRef,
            scheme: URIRef,
            notation: object,
            label: object,
            definition: object = None,
            *,
            category: str = "concepts",
            alternative: object = None,
            provenance: dict[str, Any] | None = None,
        ) -> None:
            self.add(graph, concept, RDF.type, SKOS.Concept, provenance=provenance)
            self.add(graph, concept, SKOS.inScheme, scheme, provenance=provenance)
            add_literal_properties(self.dataset, graph, concept, [
                (SKOS.notation, notation, XSD.string),
                (SKOS.prefLabel, label, None),
                (SKOS.definition, definition, None),
                (SKOS.altLabel, alternative, None),
                (EX.vocabularyVersion, "operations-vocabulary-v1", XSD.string),
            ])
            self.vocabulary_counts[category] += 1

        if table_exists("semantic_concept"):
            for row in source_db.execute(
                "SELECT * FROM semantic_concept WHERE status='active' ORDER BY concept_key"
            ):
                concept_type = str(row["concept_type"] or "term").lower()
                scheme = VOCABULARY_SCHEME_BY_TYPE.get(concept_type, EX.TerminologyScheme)
                add_concept(
                    URIRef(EX + "concept/" + safe_segment(row["concept_key"])),
                    scheme,
                    row["concept_key"],
                    row["canonical_name"] or row["concept_key"],
                    row["description"],
                    provenance=dict(row),
                )

        if table_exists("semantic_canonical_state"):
            for row in source_db.execute(
                "SELECT * FROM semantic_canonical_state WHERE status='active' ORDER BY state_domain,sort_order,canonical_state"
            ):
                state_key = f"{row['state_domain']}/{row['canonical_state']}"
                concept = URIRef(EX + "state/" + safe_segment(row["state_domain"]) + "/" + safe_segment(row["canonical_state"]))
                add_concept(
                    concept,
                    EX.DefectStateScheme,
                    row["canonical_state"],
                    row["display_name"] or row["canonical_state"],
                    row["description"],
                    category="states",
                    provenance=dict(row),
                )
                self.add(graph, concept, RDF.type, EX.CanonicalState, provenance=dict(row))
                add_literal_properties(self.dataset, graph, concept, [
                    (EX.stateDomain, row["state_domain"], XSD.string),
                    (EX.isTerminal, bool(row["is_terminal"]), XSD.boolean),
                ])

        if table_exists("semantic_status_dictionary"):
            for row in source_db.execute(
                "SELECT * FROM semantic_status_dictionary WHERE mapping_status='approved' ORDER BY source_schema,source_table,status_id"
            ):
                concept = URIRef(EX + "status/" + safe_segment(row["source_schema"]) + "/" + safe_segment(row["source_table"]) + "/" + safe_segment(row["status_id"]))
                label = row["business_meaning"] or row["raw_status"] or "未命名状态"
                add_concept(
                    concept,
                    EX.DefectStateScheme,
                    row["raw_status"] or row["status_id"],
                    label,
                    row["mapping_notes"],
                    category="statuses",
                    alternative=row["raw_status"],
                    provenance=dict(row),
                )
                if row["canonical_state"]:
                    self.add(graph, concept, SKOS.exactMatch, URIRef(EX + "state/DEFECT/" + safe_segment(row["canonical_state"])), provenance=dict(row))
                add_literal_properties(self.dataset, graph, concept, [
                    (EX.sourceSchema, row["source_schema"], XSD.string),
                    (EX.sourceTable, row["source_table"], XSD.string),
                ])

        units: set[str] = set()
        if table_exists("semantic_object_property"):
            units.update(str(row[0]).strip() for row in source_db.execute(
                "SELECT DISTINCT unit FROM semantic_object_property WHERE status='active' AND unit IS NOT NULL AND trim(unit)<>''"
            ).fetchall())
        if table_exists("semantic_fact"):
            units.update(str(row[0]).strip() for row in source_db.execute(
                "SELECT DISTINCT unit FROM semantic_fact WHERE unit IS NOT NULL AND trim(unit)<>''"
            ).fetchall())
        for unit in sorted(units):
            concept = URIRef(EX + "unit/" + safe_segment(unit))
            add_concept(concept, EX.UnitScheme, unit, unit, category="units")

        if table_exists("semantic_fact_builder_rule"):
            for row in source_db.execute(
                "SELECT rule_key,title,output_fact_type,rule_version FROM semantic_fact_builder_rule WHERE status='enabled' ORDER BY rule_key"
            ):
                concept = URIRef(EX + "term/rule/" + safe_segment(row["rule_key"]))
                add_concept(
                    concept,
                    EX.TerminologyScheme,
                    row["rule_key"],
                    row["title"] or row["rule_key"],
                    row["output_fact_type"],
                    category="terms",
                    provenance=dict(row),
                )

    def build_objects(self, source_db: sqlite3.Connection) -> None:
        graph = self.source_graph(None)
        rows = source_db.execute("SELECT * FROM semantic_object_instance WHERE status IN ('accepted','observed','derived','current') ORDER BY object_type,object_id").fetchall()
        for row in rows:
            object_type = str(row["object_type"] or "business_object").lower()
            key = str(row["canonical_key"])
            iri = URIRef(EX + f"object/{safe_segment(object_type)}/{safe_segment(key)}")
            self.object_index[(object_type, key)] = iri
            self.add(graph, iri, RDF.type, class_iri(object_type), status=str(row["status"]))
            add_literal_properties(self.dataset, graph, iri, [
                (EX.canonicalKey, row["canonical_key"], XSD.string),
                (EX.displayName, row["display_name"], XSD.string),
                (EX.sourceNamespace, row["source_namespace"], XSD.string),
                (EX.sourceSnapshotId, row["source_snapshot_id"], XSD.string),
                (EX.status, row["status"], XSD.string),
            ])
            self.add_provenance("object_projection", self.run_id, iri, source_system=row["source_namespace"], source_table="semantic_object_instance", source_row_id=row["object_id"], source_snapshot_id=row["source_snapshot_id"], evidence=row["evidence_json"])

    def build_identity_assertions(self, source_db: sqlite3.Connection) -> None:
        graph = self.source_graph(None)
        rows = source_db.execute("SELECT * FROM semantic_identity_assertion WHERE status='accepted' AND lifecycle_status='active' ORDER BY assertion_id").fetchall()
        for row in rows:
            identity = URIRef(EX + f"identity/{safe_segment(row['assertion_id'])}")
            target = resource_iri(row["canonical_object_type"], row["canonical_object_id"], self.object_index)
            self.add(graph, identity, RDF.type, EX.IdentityAssertion, confidence=row["confidence"], provenance=dict(row))
            self.add(graph, identity, EX.assertsIdentity, target, confidence=row["confidence"], provenance=dict(row))
            add_literal_properties(self.dataset, graph, identity, [
                (EX.sourceSystem, row["source_system"], XSD.string),
                (EX.sourceNamespace, row["source_schema"], XSD.string),
                (EX.sourceSchema, row["source_schema"], XSD.string),
                (EX.sourceTable, row["source_table"], XSD.string),
                (EX.sourceRecordId, row["source_row_id"], XSD.string),
                (EX.sourceSnapshotId, row["source_snapshot_id"], XSD.string),
                (EX.confidence, row["confidence"], XSD.decimal),
                (EX.decisionMode, row["decision_mode"], XSD.string),
                (EX.status, row["lifecycle_status"], XSD.string),
                (EX.validFrom, datetime_literal(row["valid_from"]), None),
                (EX.validTo, datetime_literal(row["valid_to"]), None),
            ])
            self.add_provenance("identity_assertion", row["assertion_id"], identity, source_system=row["source_system"], source_schema=row["source_schema"], source_table=row["source_table"], source_row_id=row["source_row_id"], source_snapshot_id=row["source_snapshot_id"], evidence=row["evidence_json"])

    def build_relations(self, source_db: sqlite3.Connection) -> None:
        graph = self.derived_graph
        rows = source_db.execute("SELECT * FROM semantic_relation_assertion WHERE status='accepted' ORDER BY relation_id").fetchall()
        for row in rows:
            subject = resource_iri(row["subject_type"], row["subject_key"], self.object_index)
            obj = resource_iri(row["object_type"], row["object_key"], self.object_index)
            predicate = URIRef(EX + relation_predicate(row["relation_type"]))
            self.add(graph, subject, predicate, obj, confidence=row["confidence"], provenance=dict(row))
            relation_class = EX.LocationAssignment if str(row["object_type"]).lower() == "location" else EX.RelationAssertion
            if row["valid_from"] or row["valid_to"] or relation_class == EX.LocationAssignment:
                assertion = URIRef(EX + "relation/" + safe_segment(row["relation_id"]))
                self.add(graph, assertion, RDF.type, relation_class, confidence=row["confidence"], provenance=dict(row))
                self.add(graph, assertion, EX.relationSubject, subject, confidence=row["confidence"], provenance=dict(row))
                self.add(graph, assertion, EX.relationObject, obj, confidence=row["confidence"], provenance=dict(row))
                self.add(graph, assertion, EX.relationPredicate, predicate, confidence=row["confidence"], provenance=dict(row))
                add_literal_properties(self.dataset, graph, assertion, [
                    (EX.validFrom, datetime_literal(row["valid_from"]), None),
                    (EX.validTo, datetime_literal(row["valid_to"]), None),
                ])
                if relation_class == EX.LocationAssignment:
                    self.add(graph, subject, EX.hasLocationAssignment, assertion, confidence=row["confidence"], provenance=dict(row))
                    self.add(graph, assertion, EX.assignmentDevice, subject, confidence=row["confidence"], provenance=dict(row))
                    self.add(graph, assertion, EX.assignmentLocation, obj, confidence=row["confidence"], provenance=dict(row))
            self.add_provenance("relation_assertion", row["relation_id"], subject, source_system=row["source_schema"], source_schema=row["source_schema"], source_table=row["source_table"], source_row_id=row["source_row_id"], source_snapshot_id=row["source_snapshot_id"], evidence=row["evidence_json"])

    def build_events(self, source_db: sqlite3.Connection) -> None:
        graph = self.source_graph(None)
        rows = source_db.execute("SELECT * FROM semantic_event WHERE status IN ('observed','accepted') ORDER BY event_id").fetchall()
        event_index: dict[str, URIRef] = {}
        for row in rows:
            event = URIRef(EX + f"event/{safe_segment(row['event_id'])}")
            event_index[str(row["event_id"])] = event
            event_type = str(row["event_type"] or "BusinessEvent")
            self.add(graph, event, RDF.type, EX.BusinessEvent, confidence=row["confidence"], provenance=dict(row))
            self.add(graph, event, RDF.type, URIRef(EX + safe_segment(event_type)), confidence=row["confidence"], provenance=dict(row))
            subject = resource_iri(row["subject_type"], row["subject_key"], self.object_index)
            self.add(graph, event, EX.hasSubject, subject, confidence=row["confidence"], provenance=dict(row))
            add_literal_properties(self.dataset, graph, event, [
                (EX.sourceSystem, row["source_schema"], XSD.string),
                (EX.sourceSchema, row["source_schema"], XSD.string),
                (EX.sourceTable, row["source_table"], XSD.string),
                (EX.sourceRecordId, row["source_row_id"], XSD.string),
                (EX.sourceSnapshotId, row["source_snapshot_id"], XSD.string),
                (EX.occurredAt, datetime_literal(row["occurred_at"]), None),
                (EX.recordedAt, datetime_literal(row["recorded_at"]), None),
                (EX.rawStatus, row["raw_status"], XSD.string),
                (EX.description, row["description"], XSD.string),
                (EX.correlationId, row["correlation_id"], XSD.string),
            ])
            self.add_provenance("event_projection", row["event_id"], event, source_system=row["source_schema"], source_schema=row["source_schema"], source_table=row["source_table"], source_row_id=row["source_row_id"], source_snapshot_id=row["source_snapshot_id"], evidence=row["payload_json"])
        relation_rows = source_db.execute("SELECT * FROM semantic_event_relation WHERE status='accepted' ORDER BY relation_id").fetchall()
        for row in relation_rows:
            subject = event_index.get(str(row["subject_event_id"]))
            obj = event_index.get(str(row["object_event_id"]))
            if subject is None or obj is None:
                continue
            predicate = URIRef(EX + event_predicate(row["relation_type"]))
            self.add(self.derived_graph, subject, predicate, obj, confidence=row["confidence"], provenance=dict(row))
            self.add_provenance("event_relation", row["relation_id"], subject, source_system=row["source_schema"], source_schema=row["source_schema"], source_table=row["source_table"], source_row_id=row["source_row_id"], source_snapshot_id=row["source_snapshot_id"], evidence=row["evidence_json"])

    def build_facts_states_rules(self, source_db: sqlite3.Connection) -> None:
        graph = self.derived_graph
        for row in source_db.execute("SELECT * FROM semantic_fact WHERE status IN ('observed','accepted','derived') ORDER BY fact_id"):
            fact = URIRef(EX + f"fact/{safe_segment(row['fact_id'])}")
            subject = resource_iri(row["subject_type"], row["subject_key"], self.object_index)
            self.add(graph, fact, RDF.type, EX.Fact, confidence=row["confidence"], provenance=dict(row))
            fact_class = EX.DerivedFact if str(row["status"]) == "derived" else EX.SourceFact
            self.add(graph, fact, RDF.type, fact_class, confidence=row["confidence"], provenance=dict(row))
            self.add(graph, subject, EX.hasFact, fact, confidence=row["confidence"], provenance=dict(row))
            predicate_label = str(row["predicate"] or "relatedTo")
            predicate_iri = URIRef(EX + "predicate/" + safe_segment(predicate_label))
            self.add(graph, predicate_iri, RDF.type, URIRef("http://www.w3.org/1999/02/22-rdf-syntax-ns#Property"), provenance=dict(row))
            self.add(graph, fact, EX.predicate, predicate_iri, provenance=dict(row))
            add_literal_properties(self.dataset, graph, fact, [
                (EX.factType, row["fact_type"], XSD.string),
                (EX.predicateLabel, predicate_label, XSD.string),
                (EX.valueJson, row["value_json"], XSD.string),
                (EX.unit, row["unit"], XSD.string),
                (EX.sourceSchema, row["source_schema"], XSD.string),
                (EX.sourceTable, row["source_table"], XSD.string),
                (EX.sourceRecordId, row["source_row_id"], XSD.string),
                (EX.sourceSnapshotId, row["source_snapshot_id"], XSD.string),
            ])
            if str(row["status"]) == "derived" and table_exists(source_db, "semantic_fact_derivation"):
                derivations = source_db.execute(
                    "SELECT * FROM semantic_fact_derivation WHERE output_fact_id=? AND status='accepted' ORDER BY derivation_id",
                    (row["fact_id"],),
                ).fetchall()
                for derivation in derivations:
                    activity = URIRef(EX + "derivation/" + safe_segment(derivation["derivation_id"]))
                    self.add(graph, activity, RDF.type, PROV.Activity, provenance=dict(derivation))
                    self.add(graph, fact, PROV.wasGeneratedBy, activity, provenance=dict(derivation))
                    self.add(graph, activity, EX.derivationId, Literal(str(derivation["derivation_id"]), datatype=XSD.string), provenance=dict(derivation))
                    try:
                        input_fact_ids = json.loads(derivation["input_fact_ids_json"] or "[]")
                    except (TypeError, json.JSONDecodeError):
                        input_fact_ids = []
                    if isinstance(input_fact_ids, list):
                        for input_fact_id in input_fact_ids:
                            input_fact = URIRef(EX + "fact/" + safe_segment(input_fact_id))
                            self.add(graph, fact, EX.derivedFromFact, input_fact, provenance=dict(derivation))
            self.add_provenance("fact_projection", row["fact_id"], fact, source_system=row["source_schema"], source_schema=row["source_schema"], source_table=row["source_table"], source_row_id=row["source_row_id"], source_snapshot_id=row["source_snapshot_id"], evidence=row["value_json"])
        for row in source_db.execute("SELECT * FROM semantic_current_state WHERE status='current' ORDER BY subject_key,state_domain"):
            subject = resource_iri(row["subject_type"], row["subject_key"], self.object_index)
            state = URIRef(EX + "state/" + safe_segment(row["state_domain"]) + "/" + safe_segment(row["current_state"]))
            self.add(graph, state, RDF.type, EX.CanonicalState, provenance=dict(row))
            self.add(graph, subject, EX.hasCurrentState, state, provenance=dict(row))
        for row in source_db.execute("SELECT * FROM semantic_executable_rule WHERE status IN ('replayed','approved','enabled') ORDER BY rule_id,rule_version"):
            rule = URIRef(EX + f"rule/{safe_segment(row['rule_id'])}/{safe_segment(row['rule_version'])}")
            self.add(graph, rule, RDF.type, EX.Rule, provenance=dict(row))
            add_literal_properties(self.dataset, graph, rule, [
                (EX.ruleVersion, row["rule_version"], XSD.string),
                (EX.displayName, row["title"], XSD.string),
                (EX.status, row["status"], XSD.string),
                (EX.targetObjectType, row["target_object_type"], XSD.string),
                (EX.sourceSnapshotId, row["source_version_id"], XSD.string),
            ])
            self.add_provenance("rule_projection", row["rule_id"], rule, source_system="local", source_table="semantic_executable_rule", source_row_id=row["rule_id"], source_snapshot_id=row["source_version_id"], evidence=row["provenance_json"])
        has_action_plan = source_db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='semantic_action_plan'").fetchone() is not None
        if has_action_plan:
            for row in source_db.execute("SELECT * FROM semantic_action_plan ORDER BY plan_id"):
                plan = URIRef(EX + "action-plan/" + safe_segment(row["plan_id"]))
                self.add(graph, plan, RDF.type, EX.ActionPlan, provenance=dict(row))
                add_literal_properties(self.dataset, graph, plan, [
                    (EX.actionKey, row["action_key"], XSD.string),
                    (EX.actionType, row["action_type"], XSD.string),
                    (EX.targetObjectType, row["target_type"], XSD.string),
                    (EX.targetKey, row["target_key"], XSD.string),
                    (EX.reason, row["reason"], XSD.string),
                    (EX.riskLevel, row["risk_level"], XSD.string),
                    (EX.requiresApproval, bool(row["requires_approval"]), XSD.boolean),
                    (EX.sourceWrite, bool(row["source_write"]), XSD.boolean),
                    (EX.formalPublication, bool(row["formal_publication"]), XSD.boolean),
                    (EX.status, row["status"], XSD.string),
                ])
                self.add_provenance("action_plan_projection", row["plan_id"], plan, source_system="local", source_table="semantic_action_plan", source_row_id=row["plan_id"], evidence=row["payload_json"])

    def _identity_graph_rows(self) -> list[tuple[str, str, str]]:
        """Return (snapshot id, graph IRI, graph id) for streamed identity graphs."""
        return [
            (
                snapshot_id,
                graph_iri("source", self.run_id, snapshot_id),
                f"graph-{short_hash(self.run_id, graph_iri('source', self.run_id, snapshot_id))}",
            )
            for snapshot_id in self.identity_snapshot_ids
        ]

    def _identity_evidence(self) -> str:
        return json.dumps({"projection": "source_identity_seed", "semanticRelations": False}, ensure_ascii=False, sort_keys=True)

    def _stream_identity_projection(
        self,
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
        evidence = self._identity_evidence()
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
                    subject = self.identity_subject(row["unified_device_id"])
                    subject_text = str(subject)
                    if subject_text not in resources:
                        resources.add(subject_text)
                        stats["resources"] += 1
                    stats["devices"] += 1
                    for predicate, obj in self.identity_statement_specs(row):
                        object_kind = "iri" if isinstance(obj, URIRef) else "literal"
                        object_iri = str(obj) if object_kind == "iri" else None
                        lexical_value = None if object_kind == "iri" else str(obj)
                        datatype_iri = str(obj.datatype) if isinstance(obj, Literal) and obj.datatype else None
                        language_tag = obj.language if isinstance(obj, Literal) else None
                        statement_id = f"stmt-{short_hash(self.run_id, graph_iri_value, subject, predicate, object_kind, object_iri, lexical_value, datatype_iri, language_tag)}"
                        statement_rows.append((
                            statement_id, self.run_id, graph_id, subject_text, str(predicate), object_kind,
                            object_iri, lexical_value, datatype_iri, language_tag, None, "seed", created_at,
                        ))
                        provenance_rows.append((
                            f"prov-{short_hash(statement_id, row['source_identity_key'])}", statement_id,
                            row["master_source_schema"], row["master_source_schema"], "identity.unified_device",
                            row["source_identity_key"], row["source_snapshot_id"], "canonical_projection",
                            self.run_id, evidence, created_at,
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

    def _write_identity_trig(
        self,
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
                    subject = self.identity_subject(row["unified_device_id"]).n3()
                    values = [
                        ("a", URIRef(EX + "Device").n3()),
                        (URIRef(EX + "canonicalKey").n3(), literal(row["unified_device_id"], XSD.string).n3()),
                        (URIRef(EX + "displayName").n3(), literal(self.identity_display_name(row), XSD.string).n3()),
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

    def _identity_json_node(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": str(self.identity_subject(row["unified_device_id"])),
            "type": "Device",
            "canonicalKey": str(row["unified_device_id"]),
            "displayName": self.identity_display_name(row),
            "sourceNamespace": str(row["master_source_schema"]),
            "sourceSnapshotId": str(row["source_snapshot_id"]),
            "sourceRecordId": str(row["source_identity_key"]),
        }

    def _write_identity_jsonld(
        self,
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
                    output.write(json.dumps(self._identity_json_node(row), ensure_ascii=False, separators=(",", ":")))
                    if written % 100000 == 0:
                        print(f"[canonical] wrote JSON-LD identity devices: {written}", flush=True)
            output.write("]}")

    def serialize_and_persist(
        self,
        db: sqlite3.Connection,
        issues: list[dict[str, str]],
        identity_db: sqlite3.Connection | None = None,
    ) -> dict[str, Any]:
        run_dir = RUN_ROOT / self.run_id
        run_dir.mkdir(parents=True, exist_ok=False)
        trig_path = run_dir / "canonical.trig"
        jsonld_path = run_dir / "canonical.jsonld"
        reasoning_trig_path = run_dir / "canonical.reasoning.trig"
        reasoning_jsonld_path = run_dir / "canonical.reasoning.jsonld"
        base_trig_path = run_dir / "canonical.base.trig"
        base_jsonld_path = run_dir / "canonical.base.jsonld"
        manifest_path = run_dir / "projection_manifest.json"
        validation_path = run_dir / "shacl_validation_report.json"

        # The bounded semantic graph is also the reasoning input.  Full source
        # identity rows are appended as a streamed graph below; they do not
        # create inferred relations or states merely by being present.
        self.dataset.serialize(destination=str(base_trig_path), format="trig")
        self.dataset.serialize(
            destination=str(base_jsonld_path),
            format="json-ld",
            context=load_jsonld_context(),
            auto_compact=True,
        )
        shutil.copyfile(base_trig_path, reasoning_trig_path)
        shutil.copyfile(base_jsonld_path, reasoning_jsonld_path)

        graph_map = {str(context.identifier): context for context in self.dataset.contexts()}
        graph_rows: dict[str, str] = {}
        graph_kinds: dict[str, str] = {}
        for graph_iri_value in graph_map:
            kind = "ontology" if "/ontology/" in graph_iri_value else "source" if "/source/" in graph_iri_value else "derived" if "/derived/" in graph_iri_value else "provenance"
            graph_id = f"graph-{short_hash(self.run_id, graph_iri_value)}"
            graph_rows[graph_iri_value] = graph_id
            graph_kinds[graph_iri_value] = kind
            snapshot_id = graph_iri_value.rsplit("/", 1)[-1] if kind == "source" else None
            db.execute(
                "INSERT INTO canonical_graph(graph_id,run_id,graph_iri,graph_kind,source_snapshot_id,status,created_at) VALUES (?,?,?,?,?,?,?)",
                (graph_id, self.run_id, graph_iri_value, kind, snapshot_id, "active", self.created_at),
            )

        identity_graph_rows = self._identity_graph_rows() if identity_db is not None and self.identity_preflight.get("status") == "ready" else []
        for snapshot_id, graph_iri_value, graph_id in identity_graph_rows:
            graph_rows[graph_iri_value] = graph_id
            graph_kinds[graph_iri_value] = "source"
            db.execute(
                "INSERT INTO canonical_graph(graph_id,run_id,graph_iri,graph_kind,source_snapshot_id,status,created_at) VALUES (?,?,?,?,?,?,?)",
                (graph_id, self.run_id, graph_iri_value, "source", snapshot_id, "active", self.created_at),
            )

        statements = 0
        resources: set[str] = set()
        provenance_eligible = 0
        provenance_covered = 0
        provenance_by_graph: dict[str, dict[str, int]] = {}
        for context in self.dataset.contexts():
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
                sid = f"stmt-{short_hash(self.run_id, graph_iri_value, subject, predicate, object_kind, object_iri, lexical_value, datatype_iri, language_tag)}"
                context_info = self.statement_context.get((subject, predicate, obj), {})
                statement_provenance = context_info.get("provenance") or self.subject_provenance.get(subject)
                if graph_kind in {"source", "derived"}:
                    provenance_eligible += 1
                    graph_metrics["eligible"] += 1
                    if statement_provenance:
                        provenance_covered += 1
                        graph_metrics["covered"] += 1
                db.execute(
                    "INSERT OR IGNORE INTO canonical_statement(statement_id,run_id,graph_id,subject_iri,predicate_iri,object_kind,object_iri,lexical_value,datatype_iri,language_tag,confidence,assertion_status,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (sid, self.run_id, graph_id, str(subject), str(predicate), object_kind, object_iri, lexical_value, datatype_iri, language_tag, context_info.get("confidence"), context_info.get("status", "accepted"), self.created_at),
                )
                provenance = statement_provenance or {}
                if provenance:
                    db.execute(
                        "INSERT OR IGNORE INTO canonical_provenance(provenance_id,statement_id,source_system,source_schema,source_table,source_row_id,source_snapshot_id,activity_type,activity_id,evidence_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (f"prov-{short_hash(sid, provenance.get('source_row_id'), provenance.get('event_id'), provenance.get('fact_id'))}", sid, provenance.get("source_system"), provenance.get("source_schema"), provenance.get("source_table"), provenance.get("source_row_id"), provenance.get("source_snapshot_id"), "canonical_projection", self.run_id, json_text(provenance.get("evidence_json") or provenance), self.created_at),
                    )
                statements += 1
                resources.add(str(subject))
                if object_kind == "iri":
                    resources.add(str(obj))

        identity_stats = {"devices": 0, "statements": 0, "resources": 0, "provenanceEligible": 0, "provenanceCovered": 0}
        if identity_db is not None and identity_graph_rows:
            identity_stats = self._stream_identity_projection(db, identity_db, identity_graph_rows, resources, self.created_at)
            statements += identity_stats["statements"]
            provenance_eligible += identity_stats["provenanceEligible"]
            provenance_covered += identity_stats["provenanceCovered"]
            source_metrics = provenance_by_graph.setdefault("source", {"eligible": 0, "covered": 0})
            source_metrics["eligible"] += identity_stats["provenanceEligible"]
            source_metrics["covered"] += identity_stats["provenanceCovered"]

        # Assemble the final standard artifacts without materialising the
        # source identity graph.  The base JSON-LD stays compact and the
        # identity graph is emitted as one node per line inside @graph.
        base_jsonld = json.loads(base_jsonld_path.read_text(encoding="utf-8"))
        base_context = base_jsonld.get("@context", load_jsonld_context())
        base_graphs = base_jsonld.get("@graph", [])
        with trig_path.open("w", encoding="utf-8", newline="\n") as output:
            output.write(base_trig_path.read_text(encoding="utf-8"))
            if identity_db is not None and identity_graph_rows:
                self._write_identity_trig(output, identity_db, identity_graph_rows)
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
                self._write_identity_jsonld(output, identity_db, identity_graph_rows)
            output.write("]}")
        base_trig_path.unlink(missing_ok=True)
        base_jsonld_path.unlink(missing_ok=True)

        manifest = {
            "schemaVersion": "canonical-semantic-model-v1",
            "runId": self.run_id,
            "sourceDb": str(self.source),
            "sourceSnapshotId": self.source_snapshot_id,
            "ontologyVersion": self.ontology_version,
            "standardBaseline": ["RDF 1.1", "RDFS 1.1", "OWL 2 RL", "SKOS", "SHACL 1.0", "SPARQL 1.1", "PROV-O", "JSON-LD 1.1"],
            "graphCount": len(graph_rows),
            "resourceCount": len(resources),
            "statementCount": statements,
            "validationErrorCount": len(issues),
            "sourceWrite": False,
            "formalPublication": False,
            "vocabulary": self.vocabulary_counts,
            "canonicalScope": {
                "semanticOverlayObjects": "semantic_object_instance",
                "sourceIdentityGraph": "all unified_device rows from the latest read-only identity snapshot",
                "sourceIdentityDeviceCount": int(identity_stats["devices"]),
                "sourceIdentitySnapshots": self.identity_snapshot_ids,
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
                "trig": str(trig_path),
                "jsonld": str(jsonld_path),
                "reasoningTrig": str(reasoning_trig_path),
                "reasoningJsonLd": str(reasoning_jsonld_path),
                "validation": str(validation_path),
                "rdfDatasetManifest": str(STANDARD_ROOT / str(self.version_spec.get("rdfDatasetManifest", "rdf-dataset.json"))),
                "assetManifest": str(STANDARD_ROOT / str(self.version_spec.get("assetManifest", "semantic-asset-manifest.json"))),
            },
        }
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        validation_path.write_text(json.dumps({"conforms": not issues, "issues": issues, "identityPreflight": self.identity_preflight}, ensure_ascii=False, indent=2), encoding="utf-8")
        db.execute(
            "INSERT INTO canonical_projection_run(run_id,semantic_source_db,source_snapshot_id,ontology_version,graph_count,resource_count,statement_count,validation_error_count,status,manifest_json,source_write,formal_publication,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (self.run_id, str(self.source), self.source_snapshot_id, self.ontology_version, len(graph_rows), len(resources), statements, len(issues), "completed" if not issues else "completed_with_errors", json_text(manifest), 0, 0, self.created_at),
        )
        db.commit()
        return manifest

    def run(self) -> dict[str, Any]:
        if not self.source.exists():
            raise FileNotFoundError(self.source)
        self.target.parent.mkdir(parents=True, exist_ok=True)
        source_db = sqlite3.connect(f"file:{self.source.resolve()}?mode=ro", uri=True)
        source_db.row_factory = sqlite3.Row
        source_db.execute("PRAGMA query_only=ON")
        identity_db: sqlite3.Connection | None = None
        if self.identity_source and self.identity_source.exists():
            identity_db = sqlite3.connect(f"file:{self.identity_source.resolve()}?mode=ro", uri=True)
            identity_db.row_factory = sqlite3.Row
            identity_db.execute("PRAGMA query_only=ON")
        target_db = sqlite3.connect(str(self.target), timeout=30)
        try:
            snapshot = source_db.execute(
                "SELECT source_snapshot_id,count(*) AS row_count FROM semantic_object_instance WHERE source_snapshot_id IS NOT NULL GROUP BY source_snapshot_id ORDER BY row_count DESC,source_snapshot_id LIMIT 1"
            ).fetchone()
            self.source_snapshot_id = str(snapshot["source_snapshot_id"]) if snapshot else None
            self.prepare_identity_projection(identity_db)
            ensure_schema(target_db)
            self.build_ontology(source_db)
            self.build_objects(source_db)
            self.build_identity_assertions(source_db)
            self.build_relations(source_db)
            self.build_events(source_db)
            self.build_facts_states_rules(source_db)
            issues = validate_shapes(self.dataset)
            if self.identity_preflight.get("enabled") and self.identity_preflight.get("status") != "ready":
                issues.append({
                    "focusNode": "identity://unified_device",
                    "path": "identity://preflight",
                    "code": "identityPreflight",
                    "severity": "Violation",
                    "message": "全量统一设备身份投影预检未通过，未写入来源身份图",
                })
            return self.serialize_and_persist(target_db, issues, identity_db)
        finally:
            source_db.close()
            if identity_db is not None:
                identity_db.close()
            target_db.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Build canonical RDF semantic model from the local read-only semantic runtime")
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--target", type=Path, default=DEFAULT_TARGET)
    parser.add_argument(
        "--identity-source",
        type=Path,
        default=latest_identity_source(),
        help="latest local read-only identity result; all unified_device rows are streamed into the source identity graph",
    )
    parser.add_argument(
        "--skip-full-identity",
        action="store_true",
        help="only for bounded development runs; do not use for the production Canonical RDF cutover",
    )
    args = parser.parse_args()
    identity_source = None if args.skip_full_identity else args.identity_source
    manifest = CanonicalBuilder(args.source, args.target, identity_source=identity_source).run()
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0 if manifest["validationErrorCount"] == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
