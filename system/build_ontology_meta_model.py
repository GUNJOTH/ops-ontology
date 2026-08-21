"""Build the local ontology metadata registry for the Canonical RDF model.

The Canonical RDF Dataset, ontology, SHACL shapes, vocabularies and SPARQL
contracts are the standard semantic assets. This additive registry supports
mapping, governance, replay and approval runtime concerns; it never changes
DM8/MaxiEAM, HD_SAAS, XNY_SAAS, or the source snapshots. Existing runtime rows
are not deleted or silently reclassified; unregistered predicates and event
types are surfaced as ``needs_review``.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import sqlite3
from datetime import datetime, timezone

from pipeline.contracts import connect_local
from rdflib import Graph, URIRef
from rdflib.namespace import OWL, RDF, RDFS
from semantic_namespaces import ONTOLOGY_NAMESPACE
from semantic_packages import verify_package_registry
from semantic_registry import (
    OBJECT_CLASS_LOCAL_NAMES,
    RELATION_REGISTRY,
    active_ontology_path,
    canonical_relation_key,
    object_class_local_name,
    relation_predicate_local_name,
)

ROOT = pathlib.Path(__file__).resolve().parent
DEFAULT_TARGET = ROOT / "data" / "unified_semantics.sqlite3"
CANONICAL_NAMESPACE = ONTOLOGY_NAMESPACE


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sid(prefix: str, *parts: object) -> str:
    raw = "|".join("" if part is None else str(part) for part in parts)
    return f"{prefix}-{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:24]}"


CORE_OBJECT_TYPES = (
    ("organization", "组织", "组织业务对象根类型", "abstract", 0),
    ("company", "分公司", "组织下的分公司", "entity", 1),
    ("center", "中心", "负责站点和设备的中心组织", "entity", 1),
    ("team", "责任班组", "负责巡检、消缺或工单执行的班组", "entity", 1),
    ("physical_object", "物理对象", "站点和设备的物理对象根类型", "abstract", 0),
    ("site", "站点", "设备运行所属站点", "entity", 1),
    ("device", "设备", "由源域身份唯一定位的设备业务对象", "entity", 1),
    ("location", "位置", "设备所在的功能位置和位置层级", "entity", 1),
    ("specialty", "专业", "设备、缺陷和工单对应的专业分类", "entity", 1),
    ("business_event", "业务事件", "巡检、缺陷、消缺和工单事件根类型", "abstract", 0),
    ("inspection", "巡检", "一次巡检业务记录或过程对象", "entity", 1),
    ("abnormal_inspection", "异常巡检", "巡检中产生且必须有测量或现象证据的异常对象", "entity", 1),
    ("defect", "缺陷", "经业务流程登记或确认的缺陷对象", "entity", 1),
    ("defect_resolution", "消缺", "对缺陷采取的处理和验收对象", "entity", 1),
    ("work_order", "工单", "组织执行维护工作的工单对象", "entity", 1),
    ("work_permit", "工作票", "作业许可、风险和安全措施对象", "entity", 1),
    ("human_review", "人工审核", "对事实、判断或行动计划的治理审核对象", "entity", 1),
    ("knowledge", "知识", "标准、规则、SOP 和风险知识的统一上位类", "abstract", 0),
    ("business_knowledge", "业务知识", "标准、规则、SOP 和风险知识根类型", "abstract", 0),
    ("knowledge_asset", "知识资产", "具有身份、来源、版本和审核状态的知识资产", "knowledge", 1),
    ("standard", "标准", "业务标准知识资产", "knowledge", 1),
    ("rule", "规则", "可判断、可计算、可约束、可行动的规则", "knowledge", 1),
    ("sop", "SOP", "标准作业程序知识资产", "knowledge", 1),
    ("risk", "风险", "风险知识资产", "knowledge", 1),
    ("rule_decision", "规则判断", "规则对业务事实的可解释判断结果", "knowledge", 1),
    ("semantic_fact", "语义事实", "来源或规则推导出的可追溯事实", "entity", 1),
    ("action", "行动", "统一业务 Action 资产：业务含义、前置条件、输入事实、权限、适配器映射和效果", "knowledge", 1),
    ("action_execution", "行动执行", "Action 从 requested 到 executing/succeeded/failed 的本地执行记录", "entity", 1),
    ("action_adapter", "行动适配器", "Action 到 MCP/Workflow/API 的系统能力映射", "entity", 1),
)


def load_owl_class_hierarchy() -> dict[str, str | None]:
    """Read the class tree from ontology.ttl; never maintain parents here."""
    ontology_path = active_ontology_path()
    graph = Graph()
    graph.parse(str(ontology_path), format="turtle")
    reverse = {rdf_local: object_type for object_type, rdf_local in OBJECT_CLASS_LOCAL_NAMES.items() if object_type != "fact"}
    hierarchy: dict[str, str | None] = {}
    for object_type, rdf_local in OBJECT_CLASS_LOCAL_NAMES.items():
        if object_type == "fact":
            continue
        class_iri = URIRef(CANONICAL_NAMESPACE + rdf_local)
        if (class_iri, RDF.type, OWL.Class) not in graph:
            raise ValueError(f"OWL 类未注册到 ontology.ttl: {rdf_local}")
        parents = sorted(
            str(parent)[len(CANONICAL_NAMESPACE):]
            for parent in graph.objects(class_iri, RDFS.subClassOf)
            if str(parent).startswith(CANONICAL_NAMESPACE)
        )
        if len(parents) > 1:
            raise ValueError(f"当前运行层要求单一上位类，但 {rdf_local} 有多个 OWL 父类: {parents}")
        hierarchy[object_type] = reverse.get(parents[0]) if parents else None
    return hierarchy


def ontology_object_rows() -> list[tuple[str, str, str, str | None, str, int]]:
    """Combine human-facing metadata with the OWL-derived parent hierarchy."""
    hierarchy = load_owl_class_hierarchy()
    metadata = {
        object_type: (display_name, description, kind, instantiable)
        for object_type, display_name, description, kind, instantiable in CORE_OBJECT_TYPES
    }
    abstract_classes = {"BusinessObject", "Assertion", "Organization", "PhysicalObject", "BusinessEvent", "Fact", "Knowledge", "KnowledgeAsset", "BusinessKnowledge"}
    for object_type, rdf_local in OBJECT_CLASS_LOCAL_NAMES.items():
        if object_type == "fact" or object_type in metadata:
            continue
        kind = "event" if rdf_local.endswith("Event") or rdf_local in {"BusinessEvent", "DefectResolution", "WorkPermit", "HumanReview"} else "entity"
        if rdf_local in abstract_classes:
            kind = "abstract"
        elif rdf_local in {"KnowledgeAsset", "BusinessKnowledge", "Standard", "SOP", "Risk", "Rule", "RuleDecision", "Action"}:
            kind = "knowledge"
        metadata[object_type] = (rdf_local, f"由 ontology.ttl 定义的 {rdf_local} 类", kind, int(kind != "abstract"))
    rows = []
    for object_type, (display_name, description, kind, instantiable) in sorted(metadata.items()):
        rows.append((object_type, display_name, description, hierarchy.get(object_type), kind, instantiable))
    return rows

PROPERTY_TYPES = (
    ("source_identity_key", "device", "string", None, 1, 1, 1, "source_schema + SITEID + ASSETNUM", "non_empty"),
    ("asset_number", "device", "string", None, 1, 1, 1, "ASSET.ASSETNUM", "non_empty"),
    ("canonical_name", "device", "string", None, 1, 1, 1, "ASSET.DESCRIPTION", "non_empty"),
    ("kks", "device", "code", None, 0, 0, 1, "ASSET/KKS evidence", "preserve_source_value"),
    ("location_code", "device", "code", None, 0, 0, 1, "ASSET.LOCATION", "preserve_source_value"),
    ("parent_asset_number", "device", "string", None, 0, 0, 1, "ASSET.PARENT", "preserve_source_value"),
    ("classification_id", "device", "code", None, 0, 0, 1, "ASSET.CLASSSTRUCTUREID", "preserve_source_value"),
    ("temperature", "inspection", "decimal", "temperature", 0, 0, 1, "巡检测量字段", "numeric"),
    ("event_time", "business_event", "datetime", None, 1, 1, 1, "源事件时间字段", "iso_datetime"),
    ("defect_status", "defect", "code", None, 0, 0, 1, "TICKET/SR 状态字段", "registered_status_only"),
    ("risk_score", "risk", "decimal", "risk", 0, 1, 1, "risk_assessment Fact", "0<=x<=5"),
    ("action_name", "action", "string", None, 1, 1, 1, "semantic_action_definition.action_name", "non_empty"),
    ("business_meaning", "action", "string", None, 1, 1, 1, "semantic_action_definition.business_meaning", "non_empty"),
    ("allowed_when", "action", "json", None, 0, 0, 1, "semantic_action_definition.allowed_when_json", "json"),
    ("required_input_facts", "action", "json", None, 0, 0, 1, "semantic_action_definition.required_facts_json", "json"),
    ("permission_scope", "action", "json", None, 0, 0, 1, "semantic_action_definition.permission_scope_json", "json"),
    ("adapter_mappings", "action", "json", None, 0, 0, 1, "semantic_action_definition.adapter_mappings_json", "json"),
    ("effects", "action", "json", None, 0, 0, 1, "semantic_action_definition.effects_json", "json"),
    ("execution_states", "action", "json", None, 0, 0, 1, "semantic_action_definition.execution_states_json", "json"),
)

RELATION_TYPES = (
    ("device_located_at", "device", "location", None, 0, 1, 1, 0, 0),
    ("device_belongs_to_site", "device", "site", None, 0, 1, 1, 0, 0),
    ("device_managed_by_team", "device", "team", None, 0, 1, 1, 0, 0),
    ("device_has_inspection", "device", "inspection", None, 0, None, 1, 0, 0),
    ("device_has_defect", "device", "defect", None, 0, None, 1, 0, 0),
    ("device_has_work_order", "device", "work_order", None, 0, None, 1, 0, 0),
    ("defect_generated_from", "defect", "abnormal_inspection", None, 0, None, 1, 0, 0),
    ("defect_resolved_by", "defect", "defect_resolution", None, 0, None, 1, 0, 0),
    ("defect_has_work_order", "defect", "work_order", None, 0, None, 1, 0, 0),
    ("work_order_has_permit", "work_order", "work_permit", None, 0, None, 1, 0, 0),
    ("governed_by_rule", "semantic_fact", "rule", None, 0, None, 1, 0, 0),
    ("derived_from_fact", "semantic_fact", "semantic_fact", None, 0, None, 0, 0, 0),
    ("team_responsible_for_device", "team", "device", "managedByTeam", 0, None, 1, 0, 0),
    ("device_parent_of", "device", "device", "parent_device", 0, None, 0, 1, 0),
    ("recorded_for", "business_event", "device", "hasSubject", 0, 1, 1, 0, 0),
    ("governs_device_semantics", "knowledge_asset", "device", "governedByKnowledge", 0, None, 1, 0, 0),
    ("evaluates_device", "knowledge_asset", "device", "evaluatedByKnowledge", 0, None, 1, 0, 0),
    ("device_same_function_location", "device", "device", None, 0, None, 1, 0, 1),
)

EVENT_TYPES = (
    ("InspectionEvent", "巡检事件", "inspection", 0, "巡检记录"),
    ("AbnormalInspectionEvent", "异常巡检事件", "abnormal_inspection", 1, "FAULTFLAG/异常描述"),
    ("DefectEvent", "缺陷事件", "defect", 1, "TICKET/SR 状态事件"),
    ("DefectCreatedEvent", "缺陷创建事件", "defect", 1, "TICKET/SR"),
    ("DefectAcceptedEvent", "缺陷确认事件", "defect", 1, "缺陷流程"),
    ("DefectProcessingEvent", "缺陷处理事件", "defect", 1, "缺陷流程"),
    ("ResolutionEvent", "消缺事件", "defect_resolution", 1, "缺陷处理/验收字段"),
    ("DefectAcceptanceEvent", "缺陷验收事件", "defect", 1, "运行验收"),
    ("WorkOrderEvent", "工单事件", "work_order", 1, "WORKORDER"),
    ("WorkPermitEvent", "工作票事件", "work_permit", 1, "工作票"),
    ("HumanReviewEvent", "人工审核事件", "human_review", 1, "本地审核台账"),
)

TRANSITIONS = (
    (None, "DefectCreatedEvent", {}, "NEW", 10),
    ("NEW", "DefectAcceptedEvent", {"guard_key": "review_accept_evidence"}, "PENDING", 10),
    ("PENDING", "DefectProcessingEvent", {"guard_key": "team_assignment_present"}, "PROCESSING", 10),
    ("PROCESSING", "ResolutionEvent", {"guard_key": "resolution_evidence_present"}, "RESOLVED", 10),
    ("RESOLVED", "DefectAcceptanceEvent", {"guard_key": "acceptance_pass"}, "CLOSED", 10),
    ("CLOSED", "DefectProcessingEvent", {"guard_key": "restore_event"}, "PROCESSING", 1),
    ("CANCELLED", "DefectProcessingEvent", {"guard_key": "restore_event"}, "PROCESSING", 1),
)


def ensure_schema(db: sqlite3.Connection) -> None:
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS ontology_object_type (
          object_type TEXT PRIMARY KEY, namespace TEXT NOT NULL, display_name TEXT NOT NULL, description TEXT NOT NULL,
          rdf_class TEXT,
          parent_object_type TEXT, kind TEXT NOT NULL CHECK(kind IN ('entity','event','knowledge','abstract')),
          instantiable INTEGER NOT NULL CHECK(instantiable IN (0,1)), version TEXT NOT NULL,
          review_status TEXT NOT NULL CHECK(review_status IN ('draft','approved','needs_review','rejected')),
          status TEXT NOT NULL CHECK(status IN ('active','retired')), created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS ontology_property_type (
          property_key TEXT PRIMARY KEY, domain_type TEXT NOT NULL, value_type TEXT NOT NULL,
          unit_dimension TEXT, required INTEGER NOT NULL CHECK(required IN (0,1)),
          min_count INTEGER NOT NULL, max_count INTEGER, source_mapping TEXT NOT NULL,
          validation_rule TEXT NOT NULL, version TEXT NOT NULL,
          review_status TEXT NOT NULL CHECK(review_status IN ('draft','approved','needs_review','rejected')),
          status TEXT NOT NULL CHECK(status IN ('active','retired')), created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS ontology_relation_type (
          predicate TEXT PRIMARY KEY, domain_type TEXT NOT NULL, range_type TEXT NOT NULL,
          rdf_predicate TEXT,
          inverse_predicate TEXT, min_cardinality INTEGER NOT NULL, max_cardinality INTEGER,
          temporal INTEGER NOT NULL CHECK(temporal IN (0,1)), transitive INTEGER NOT NULL CHECK(transitive IN (0,1)),
          symmetric INTEGER NOT NULL CHECK(symmetric IN (0,1)), version TEXT NOT NULL,
          review_status TEXT NOT NULL CHECK(review_status IN ('draft','approved','needs_review','rejected')),
          status TEXT NOT NULL CHECK(status IN ('active','retired')), created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS ontology_event_type (
          event_type TEXT PRIMARY KEY, display_name TEXT NOT NULL, subject_type TEXT NOT NULL,
          rdf_class TEXT,
          affects_state INTEGER NOT NULL CHECK(affects_state IN (0,1)), source_mapping TEXT NOT NULL,
          version TEXT NOT NULL, review_status TEXT NOT NULL CHECK(review_status IN ('draft','approved','needs_review','rejected')),
          status TEXT NOT NULL CHECK(status IN ('active','retired')), created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS ontology_state_machine (
          machine_id TEXT PRIMARY KEY, domain TEXT NOT NULL, initial_state TEXT, version TEXT NOT NULL,
          review_status TEXT NOT NULL CHECK(review_status IN ('draft','approved','needs_review','rejected')),
          status TEXT NOT NULL CHECK(status IN ('active','retired')), created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS ontology_transition_rule (
          transition_rule_id TEXT PRIMARY KEY, machine_id TEXT NOT NULL REFERENCES ontology_state_machine(machine_id),
          from_state TEXT, event_type TEXT NOT NULL, guard_json TEXT NOT NULL, to_state TEXT NOT NULL,
          event_iri TEXT,
          priority INTEGER NOT NULL, version TEXT NOT NULL,
          review_status TEXT NOT NULL CHECK(review_status IN ('draft','approved','needs_review','rejected')),
          status TEXT NOT NULL CHECK(status IN ('active','retired')), created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS ontology_meta_model_run (
          run_id TEXT PRIMARY KEY, object_type_count INTEGER NOT NULL, property_type_count INTEGER NOT NULL,
          relation_type_count INTEGER NOT NULL, event_type_count INTEGER NOT NULL, state_machine_count INTEGER NOT NULL,
          transition_rule_count INTEGER NOT NULL, unregistered_relation_count INTEGER NOT NULL,
          unregistered_event_count INTEGER NOT NULL, source_write INTEGER NOT NULL CHECK(source_write=0),
          formal_publication INTEGER NOT NULL CHECK(formal_publication=0), created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_ontology_property_domain ON ontology_property_type(domain_type,status);
        CREATE INDEX IF NOT EXISTS ix_ontology_relation_domain_range ON ontology_relation_type(domain_type,range_type,status);
        CREATE INDEX IF NOT EXISTS ix_ontology_transition_machine ON ontology_transition_rule(machine_id,from_state,to_state,status);
        """
    )
    columns = {row[1] for row in db.execute("PRAGMA table_info(ontology_state_machine)")}
    if "manual_only_states_json" not in columns:
        db.execute("ALTER TABLE ontology_state_machine ADD COLUMN manual_only_states_json TEXT NOT NULL DEFAULT '[]'")
    relation_columns = {row[1] for row in db.execute("PRAGMA table_info(ontology_relation_type)")}
    if "rdf_predicate" not in relation_columns:
        db.execute("ALTER TABLE ontology_relation_type ADD COLUMN rdf_predicate TEXT")
    object_columns = {row[1] for row in db.execute("PRAGMA table_info(ontology_object_type)")}
    if "rdf_class" not in object_columns:
        db.execute("ALTER TABLE ontology_object_type ADD COLUMN rdf_class TEXT")
    event_columns = {row[1] for row in db.execute("PRAGMA table_info(ontology_event_type)")}
    if "rdf_class" not in event_columns:
        db.execute("ALTER TABLE ontology_event_type ADD COLUMN rdf_class TEXT")
    transition_columns = {row[1] for row in db.execute("PRAGMA table_info(ontology_transition_rule)")}
    if "event_iri" not in transition_columns:
        db.execute("ALTER TABLE ontology_transition_rule ADD COLUMN event_iri TEXT")


def build(target_path: pathlib.Path) -> dict[str, object]:
    package_contract = verify_package_registry()
    if package_contract["status"] != "PASS":
        raise ValueError("Ontology Package 组合门禁失败: " + ";".join(package_contract["failures"]))
    db = connect_local(target_path, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA busy_timeout=30000")
    ensure_schema(db)
    created = now()
    object_rows = ontology_object_rows()
    canonical_object_types = {row[0] for row in object_rows}
    placeholders = ",".join("?" for _ in canonical_object_types)
    retired_non_owl = db.execute(
        f"""UPDATE ontology_object_type
            SET status='retired', review_status='needs_review', updated_at=?
            WHERE status='active' AND object_type NOT IN ({placeholders})""",
        (created, *sorted(canonical_object_types)),
    ).rowcount
    for object_type, display_name, description, parent, kind, instantiable in object_rows:
        db.execute(
            """INSERT INTO ontology_object_type(object_type,namespace,display_name,description,parent_object_type,kind,
              rdf_class,instantiable,version,review_status,status,created_at,updated_at)
              VALUES (?,?,?,?,?,?,?,?,?,?,'active',?,?)
              ON CONFLICT(object_type) DO UPDATE SET namespace=excluded.namespace,display_name=excluded.display_name,
                description=excluded.description,parent_object_type=excluded.parent_object_type,kind=excluded.kind,
                rdf_class=excluded.rdf_class,instantiable=excluded.instantiable,version=excluded.version,
                review_status=excluded.review_status,updated_at=excluded.updated_at""",
            (object_type, CANONICAL_NAMESPACE, display_name, description, parent, kind, object_class_local_name(object_type), instantiable, "ontology-v1", "approved", created, created),
        )

    for property_key, domain_type, value_type, unit, required, min_count, max_count, source_mapping, validation in PROPERTY_TYPES:
        db.execute(
            """INSERT INTO ontology_property_type(property_key,domain_type,value_type,unit_dimension,required,min_count,max_count,
              source_mapping,validation_rule,version,review_status,status,created_at,updated_at)
              VALUES (?,?,?,?,?,?,?,?,?,'ontology-v1','approved','active',?,?)
              ON CONFLICT(property_key) DO UPDATE SET domain_type=excluded.domain_type,value_type=excluded.value_type,
                unit_dimension=excluded.unit_dimension,required=excluded.required,min_count=excluded.min_count,max_count=excluded.max_count,
                source_mapping=excluded.source_mapping,validation_rule=excluded.validation_rule,updated_at=excluded.updated_at""",
            (property_key, domain_type, value_type, unit, required, min_count, max_count, source_mapping, validation, created, created),
        )

    # Migrate the old camelCase registry keys in place.  The stable relational
    # key is now the primary registry key; rdf_predicate records its explicit
    # canonical RDF local name and prevents a second implicit vocabulary.
    for old_key, new_key in {
        "locatedAt": "device_located_at", "belongsToSite": "device_belongs_to_site",
        "managedByTeam": "device_managed_by_team", "hasInspection": "device_has_inspection",
        "hasDefect": "device_has_defect", "generatedFrom": "defect_generated_from",
        "resolvedBy": "defect_resolved_by", "hasTreatmentWorkOrder": "defect_has_work_order",
        "hasWorkOrder": "device_has_work_order", "hasWorkPermit": "work_order_has_permit", "responsibleFor": "team_responsible_for_device",
        "parentOf": "device_parent_of", "governedBy": "governed_by_rule",
        "derivedFrom": "derived_from_fact", "hasSubject": "recorded_for",
        "parent_device": "device_parent_of",
    }.items():
        old_exists = db.execute("SELECT 1 FROM ontology_relation_type WHERE predicate=?", (old_key,)).fetchone()
        new_exists = db.execute("SELECT 1 FROM ontology_relation_type WHERE predicate=?", (new_key,)).fetchone()
        if old_exists and new_exists:
            # The canonical seed below is authoritative.  Remove only the
            # duplicate legacy key; keep any existing stable row for its
            # foreign-key/API identity.
            db.execute("DELETE FROM ontology_relation_type WHERE predicate=?", (old_key,))
        elif old_exists:
            db.execute("UPDATE ontology_relation_type SET predicate=? WHERE predicate=?", (new_key, old_key))

    known_relations = {item[0] for item in RELATION_TYPES}
    for predicate, domain_type, range_type, inverse, min_cardinality, max_cardinality, temporal, transitive, symmetric in RELATION_TYPES:
        rdf_predicate = relation_predicate_local_name(predicate) if predicate in RELATION_REGISTRY else None
        db.execute(
            """INSERT INTO ontology_relation_type(predicate,domain_type,range_type,rdf_predicate,inverse_predicate,min_cardinality,max_cardinality,
              temporal,transitive,symmetric,version,review_status,status,created_at,updated_at)
              VALUES (?,?,?,?,?,?,?,?,?,?,'ontology-v1','approved','active',?,?)
              ON CONFLICT(predicate) DO UPDATE SET domain_type=excluded.domain_type,range_type=excluded.range_type,
                rdf_predicate=excluded.rdf_predicate,
                inverse_predicate=excluded.inverse_predicate,min_cardinality=excluded.min_cardinality,max_cardinality=excluded.max_cardinality,
                temporal=excluded.temporal,transitive=excluded.transitive,symmetric=excluded.symmetric,version=excluded.version,
                review_status='approved',status='active',updated_at=excluded.updated_at""",
            (predicate, domain_type, range_type, rdf_predicate, inverse, min_cardinality, max_cardinality, temporal, transitive, symmetric, created, created),
        )
    for row in db.execute("SELECT relation_id,predicate FROM business_object_relation").fetchall():
        normalized = canonical_relation_key(row["predicate"])
        if normalized and normalized != row["predicate"]:
            db.execute("UPDATE business_object_relation SET predicate=? WHERE relation_id=?", (normalized, row["relation_id"]))
    relation_rows = db.execute("SELECT predicate,subject_type,object_type FROM business_object_relation GROUP BY predicate,subject_type,object_type").fetchall()
    for row in relation_rows:
        predicate = canonical_relation_key(row["predicate"])
        if predicate in known_relations:
            continue
        db.execute(
            """INSERT INTO ontology_relation_type(predicate,domain_type,range_type,rdf_predicate,min_cardinality,max_cardinality,temporal,transitive,symmetric,
              version,review_status,status,created_at,updated_at)
              VALUES (?,?,?,NULL,0,NULL,0,0,0,'inferred-from-existing-v1','needs_review','active',?,?)
              ON CONFLICT(predicate) DO NOTHING""",
            (predicate, row["subject_type"], row["object_type"], created, created),
        )

    known_events = {item[0] for item in EVENT_TYPES}
    for event_type, display_name, subject_type, affects_state, source_mapping in EVENT_TYPES:
        db.execute(
            """INSERT INTO ontology_event_type(event_type,display_name,subject_type,rdf_class,affects_state,source_mapping,version,review_status,status,created_at,updated_at)
              VALUES (?,?,?,?,?,?,'ontology-v1','approved','active',?,?)
              ON CONFLICT(event_type) DO UPDATE SET display_name=excluded.display_name,subject_type=excluded.subject_type,
                rdf_class=excluded.rdf_class,affects_state=excluded.affects_state,source_mapping=excluded.source_mapping,version=excluded.version,
                review_status='approved',status='active',updated_at=excluded.updated_at""",
            (event_type, display_name, subject_type, CANONICAL_NAMESPACE + event_type, affects_state, source_mapping, created, created),
        )
    event_rows = db.execute("SELECT event_type,subject_type FROM semantic_event GROUP BY event_type,subject_type").fetchall()
    for row in event_rows:
        if row["event_type"] in known_events:
            continue
        db.execute(
            """INSERT INTO ontology_event_type(event_type,display_name,subject_type,rdf_class,affects_state,source_mapping,version,review_status,status,created_at,updated_at)
              VALUES (?,?,?,NULL,0,'semantic_event','inferred-from-existing-v1','needs_review','active',?,?)
              ON CONFLICT(event_type) DO NOTHING""",
            (row["event_type"], row["event_type"], row["subject_type"], created, created),
        )

    state_machine_id = "SM:DEFECT:v1"
    db.execute(
        """INSERT INTO ontology_state_machine(machine_id,domain,initial_state,version,review_status,status,created_at,updated_at,manual_only_states_json)
          VALUES (?,?,'NEW','state-machine-v1','approved','active',?,?,?)
          ON CONFLICT(machine_id) DO UPDATE SET domain=excluded.domain,initial_state=excluded.initial_state,
            version=excluded.version,manual_only_states_json=excluded.manual_only_states_json,updated_at=excluded.updated_at""",
        (state_machine_id, "DEFECT", created, created, json.dumps(["SUSPENDED", "CANCELLED", "UNKNOWN"], ensure_ascii=False)),
    )
    for from_state, event_type, guard, to_state, priority in TRANSITIONS:
        transition_id = sid("OTR", state_machine_id, from_state, event_type, to_state)
        db.execute(
            """INSERT INTO ontology_transition_rule(transition_rule_id,machine_id,from_state,event_type,guard_json,to_state,event_iri,priority,version,
              review_status,status,created_at,updated_at)
              VALUES (?,?,?,?,?,?,?,?,'state-machine-v1','approved','active',?,?)
              ON CONFLICT(transition_rule_id) DO UPDATE SET from_state=excluded.from_state,event_type=excluded.event_type,
                guard_json=excluded.guard_json,to_state=excluded.to_state,event_iri=excluded.event_iri,priority=excluded.priority,updated_at=excluded.updated_at""",
            (transition_id, state_machine_id, from_state, event_type, json.dumps(guard, ensure_ascii=False, sort_keys=True), to_state,
             CANONICAL_NAMESPACE + event_type, priority, created, created),
        )

    unregistered_relations = int(db.execute(
        """SELECT count(*) FROM (SELECT DISTINCT r.predicate FROM business_object_relation r
           LEFT JOIN ontology_relation_type t ON t.predicate=r.predicate
           WHERE r.status='accepted' AND (t.predicate IS NULL OR t.review_status='needs_review'))"""
    ).fetchone()[0])
    unregistered_events = int(db.execute(
        """SELECT count(*) FROM (SELECT DISTINCT e.event_type FROM semantic_event e
           LEFT JOIN ontology_event_type t ON t.event_type=e.event_type
           WHERE e.status IN ('observed','accepted') AND (t.event_type IS NULL OR t.review_status='needs_review'))"""
    ).fetchone()[0])
    counts = {
        "object_type_count": int(db.execute("SELECT count(*) FROM ontology_object_type WHERE status='active'").fetchone()[0]),
        "property_type_count": int(db.execute("SELECT count(*) FROM ontology_property_type WHERE status='active'").fetchone()[0]),
        "relation_type_count": int(db.execute("SELECT count(*) FROM ontology_relation_type WHERE status='active'").fetchone()[0]),
        "event_type_count": int(db.execute("SELECT count(*) FROM ontology_event_type WHERE status='active'").fetchone()[0]),
        "state_machine_count": int(db.execute("SELECT count(*) FROM ontology_state_machine WHERE status='active'").fetchone()[0]),
        "transition_rule_count": int(db.execute("SELECT count(*) FROM ontology_transition_rule WHERE status='active'").fetchone()[0]),
    }
    run_id = sid("OMR", created)
    db.execute(
        """INSERT INTO ontology_meta_model_run(run_id,object_type_count,property_type_count,relation_type_count,event_type_count,
          state_machine_count,transition_rule_count,unregistered_relation_count,unregistered_event_count,source_write,formal_publication,created_at)
          VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (run_id, counts["object_type_count"], counts["property_type_count"], counts["relation_type_count"], counts["event_type_count"],
         counts["state_machine_count"], counts["transition_rule_count"], unregistered_relations, unregistered_events, 0, 0, created),
    )
    db.commit()
    db.close()
    return {"run_id": run_id, **counts, "retired_non_owl_object_type_count": retired_non_owl,
            "unregistered_relation_count": unregistered_relations, "unregistered_event_count": unregistered_events,
            "ontology_source": str(active_ontology_path()),
            "package_composition_hash": next(
                (
                    item.get("compositionHash")
                    for item in package_contract.get("packages", [])
                    if item.get("id") == "__composition__"
                ),
                None,
            ),
            "source_write": False, "formal_publication": False}


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the local ontology meta-model registry")
    parser.add_argument("--target-db", type=pathlib.Path, default=DEFAULT_TARGET)
    args = parser.parse_args()
    print(json.dumps(build(args.target_db.resolve()), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
