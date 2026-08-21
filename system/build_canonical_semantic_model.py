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
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote

try:
    from rdflib import BNode, Dataset, Graph, Literal, Namespace, URIRef
    from rdflib.namespace import RDF, RDFS, XSD
except ImportError as exc:  # pragma: no cover - gives an actionable runtime error
    raise SystemExit(
        "缺少 rdflib，请先安装统一 Python 依赖：uv pip install --target backend/.deps -r backend/requirements.lock"
    ) from exc

from pipeline.contracts import connect_local, connect_readonly, manifest_path
from pipeline.entrypoint import PipelineStepError, add_pipeline_arguments, run_single_step
from semantic_namespaces import GRAPH_NAMESPACE, ONTOLOGY_NAMESPACE, RESOURCE_NAMESPACE, SOURCE_NAMESPACE
from semantic_registry import object_class_local_name

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

    @staticmethod
    def ensure_target_schema(db: sqlite3.Connection) -> None:
        """Pipeline adapter for the local Canonical RDF store schema."""
        ensure_schema(db)

    @staticmethod
    def validate_dataset(dataset: Dataset) -> list[dict[str, str]]:
        """Pipeline adapter for the SHACL-compatible validation gate."""
        return validate_shapes(dataset)

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
        from canonical.projection import build_ontology
        return build_ontology(self, source_db)

    def build_objects(self, source_db: sqlite3.Connection) -> None:
        from canonical.projection import build_objects
        return build_objects(self, source_db)

    def build_identity_assertions(self, source_db: sqlite3.Connection) -> None:
        from canonical.projection import build_identity_assertions
        return build_identity_assertions(self, source_db)

    def build_relations(self, source_db: sqlite3.Connection) -> None:
        from canonical.projection import build_relations
        return build_relations(self, source_db)

    def build_events(self, source_db: sqlite3.Connection) -> None:
        from canonical.projection import build_events
        return build_events(self, source_db)

    def build_facts_states_rules(self, source_db: sqlite3.Connection) -> None:
        from canonical.projection import build_facts_states_rules
        return build_facts_states_rules(self, source_db)

    def _identity_graph_rows(self) -> list[tuple[str, str, str]]:
        from canonical.identity import identity_graph_rows
        return identity_graph_rows(self)

    def _identity_evidence(self) -> str:
        from canonical.identity import identity_evidence
        return identity_evidence(self)

    def _stream_identity_projection(
        self,
        db: sqlite3.Connection,
        identity_db: sqlite3.Connection,
        graph_rows: list[tuple[str, str, str]],
        resources: set[str],
        created_at: str,
    ) -> dict[str, int]:
        from canonical.identity import stream_identity_projection
        return stream_identity_projection(self, db, identity_db, graph_rows, resources, created_at)

    def _write_identity_trig(
        self,
        output: Any,
        identity_db: sqlite3.Connection,
        graph_rows: list[tuple[str, str, str]],
    ) -> None:
        from canonical.identity import write_identity_trig
        return write_identity_trig(self, output, identity_db, graph_rows)

    def _identity_json_node(self, row: sqlite3.Row) -> dict[str, Any]:
        from canonical.identity import identity_json_node
        return identity_json_node(self, row)

    def _write_identity_jsonld(
        self,
        output: Any,
        identity_db: sqlite3.Connection,
        graph_rows: list[tuple[str, str, str]],
    ) -> None:
        from canonical.identity import write_identity_jsonld
        return write_identity_jsonld(self, output, identity_db, graph_rows)

    def serialize_and_persist(
        self,
        db: sqlite3.Connection,
        issues: list[dict[str, str]],
        identity_db: sqlite3.Connection | None = None,
    ) -> dict[str, Any]:
        """Delegate artifact writes to the isolated persistence stage."""
        from canonical.persistence import persist_canonical_dataset

        return persist_canonical_dataset(
            self,
            db,
            issues,
            identity_db,
            run_root=RUN_ROOT,
            standard_root=STANDARD_ROOT,
            manifest_path_fn=manifest_path,
            load_jsonld_context_fn=load_jsonld_context,
            short_hash_fn=short_hash,
            json_text_fn=json_text,
        )

    def run(self) -> dict[str, Any]:
        if not self.source.exists():
            raise FileNotFoundError(self.source)
        self.target.parent.mkdir(parents=True, exist_ok=True)
        source_db = connect_readonly(self.source)
        identity_db: sqlite3.Connection | None = None
        if self.identity_source and self.identity_source.exists():
            identity_db = connect_readonly(self.identity_source)
        target_db = connect_local(self.target, timeout=30)
        try:
            # The pipeline module owns stage ordering and run evidence.  The
            # builder remains responsible for domain-specific RDF projection.
            from canonical_pipeline import run_canonical_pipeline

            return run_canonical_pipeline(
                self,
                source_db,
                identity_db,
                target_db,
                root=ROOT,
            )
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
    add_pipeline_arguments(parser)
    args = parser.parse_args()
    identity_source = None if args.skip_full_identity else args.identity_source
    try:
        pipeline = run_single_step(
            pipeline_id="canonical-rdf-build",
            pipeline_version="canonical-rdf-build-v1",
            step_id="canonical_projection",
            root=ROOT,
            parameters={
                "sourceDb": str(args.source.resolve()),
                "targetDb": str(args.target.resolve()),
                "identitySource": str(identity_source.resolve()) if identity_source else None,
                "skipFullIdentity": bool(args.skip_full_identity),
            },
            handler=lambda _context, _dependencies: _build_canonical_or_fail(args.source, args.target, identity_source),
            manifest_path=args.pipeline_manifest,
            resume_manifest_path=args.resume_manifest,
        )
    except PipelineStepError as exc:
        print(json.dumps(exc.payload, ensure_ascii=False, indent=2))
        return 2
    manifest = pipeline["outputs"]["canonical_projection"]
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


def _build_canonical_or_fail(source: Path, target: Path, identity_source: Path | None) -> dict[str, Any]:
    manifest = CanonicalBuilder(source, target, identity_source=identity_source).run()
    if int(manifest.get("validationErrorCount") or 0) != 0:
        raise PipelineStepError("Canonical RDF validation failed", manifest)
    return manifest


if __name__ == "__main__":
    raise SystemExit(main())
