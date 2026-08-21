"""Single semantic vocabulary registry for the relational and RDF layers.

The local SQLite tables keep stable, readable relation keys.  The canonical
RDF model uses the explicitly registered local names below.  Keeping this
mapping in one module prevents business_object_relation, ontology metadata,
semantic_relation_contract and the RDF projection from silently inventing
different predicates or object classes.
"""
from __future__ import annotations

import json
from pathlib import Path

OBJECT_CLASS_LOCAL_NAMES: dict[str, str] = {
    "business_object": "BusinessObject",
    "assertion": "Assertion",
    "organization": "Organization",
    "company": "Company",
    "center": "Center",
    "team": "Team",
    "specialty": "Specialty",
    "physical_object": "PhysicalObject",
    "site": "Site",
    "location": "Location",
    "device": "Device",
    "inspection": "Inspection",
    "inspection_event": "InspectionEvent",
    "abnormal_inspection": "AbnormalInspection",
    "abnormal_inspection_event": "AbnormalInspectionEvent",
    "defect": "Defect",
    "defect_event": "DefectEvent",
    "defect_created_event": "DefectCreatedEvent",
    "defect_accepted_event": "DefectAcceptedEvent",
    "defect_processing_event": "DefectProcessingEvent",
    "defect_resolution": "DefectResolution",
    "resolution_event": "ResolutionEvent",
    "defect_acceptance_event": "DefectAcceptanceEvent",
    "work_order": "WorkOrder",
    "work_order_event": "WorkOrderEvent",
    "work_permit": "WorkPermit",
    "work_permit_event": "WorkPermitEvent",
    "human_review": "HumanReview",
    "human_review_event": "HumanReviewEvent",
    "observation_event": "ObservationEvent",
    "escalation_event": "EscalationEvent",
    "business_event": "BusinessEvent",
    "knowledge": "Knowledge",
    "business_knowledge": "BusinessKnowledge",
    "knowledge_asset": "KnowledgeAsset",
    "standard": "Standard",
    "rule": "Rule",
    "sop": "SOP",
    "risk": "Risk",
    "rule_decision": "RuleDecision",
    "document_knowledge": "DocumentKnowledge",
    "case_knowledge": "CaseKnowledge",
    "expert_knowledge": "ExpertKnowledge",
    "knowledge_fragment": "KnowledgeFragment",
    "knowledge_version": "KnowledgeVersion",
    "knowledge_conflict": "KnowledgeConflict",
    "knowledge_release": "KnowledgeRelease",
    "semantic_fact": "Fact",
    "fact": "Fact",
    "source_fact": "SourceFact",
    "derived_fact": "DerivedFact",
    "fact_value": "FactValue",
    "required_fact": "RequiredFact",
    "action_condition": "ActionCondition",
    "action_effect": "ActionEffect",
    "execution_state": "ExecutionState",
    "action_plan": "ActionPlan",
    "action": "Action",
    "action_execution": "ActionExecution",
    "action_adapter": "ActionAdapter",
    "repeated_defect": "RepeatedDefect",
    "severe_defect": "SevereDefect",
    "canonical_state": "CanonicalState",
    "identity_assertion": "IdentityAssertion",
    "relation_assertion": "RelationAssertion",
    "location_assignment": "LocationAssignment",
}


# Stable relational key -> (RDF local name, domain, range).
RELATION_REGISTRY: dict[str, tuple[str, str, str]] = {
    "device_has_inspection": ("hasInspection", "device", "inspection"),
    "device_has_defect": ("hasDefect", "device", "defect"),
    "device_has_work_order": ("hasWorkOrder", "device", "work_order"),
    "defect_has_work_order": ("hasTreatmentWorkOrder", "defect", "work_order"),
    "inspection_has_defect": ("detectsDefect", "inspection", "defect"),
    "device_located_at": ("locatedAt", "device", "location"),
    "device_belongs_to_site": ("belongsToSite", "device", "site"),
    "device_managed_by_team": ("managedByTeam", "device", "team"),
    "defect_generated_from": ("generatedFrom", "defect", "abnormal_inspection"),
    "defect_resolved_by": ("resolvedBy", "defect", "defect_resolution"),
    "work_order_has_permit": ("hasWorkPermit", "work_order", "work_permit"),
    "governed_by_rule": ("governedBy", "semantic_fact", "rule"),
    "derived_from_fact": ("derivedFrom", "semantic_fact", "semantic_fact"),
    "team_responsible_for_device": ("responsibleFor", "team", "device"),
    "device_parent_of": ("parentOf", "device", "device"),
    "device_same_function_location": ("sameFunctionLocation", "device", "device"),
    "device_related_to_device": ("relatedTo", "device", "device"),
    "recorded_for": ("hasSubject", "business_event", "device"),
    "governs_device_semantics": ("governedByKnowledge", "knowledge_asset", "device"),
    "evaluates_device": ("evaluatedByKnowledge", "knowledge_asset", "device"),
}


# Values persisted by unified_device_relation.predicate.  This is the single
# contract used both to generate the SQLite CHECK constraint and to verify
# that every accepted storage spelling can be resolved by the RDF registry.
RELATIONAL_STORAGE_PREDICATES: tuple[str, ...] = (
    "parent_device",
    "same_function_location",
    "related_device",
)


# Legacy/camelCase spellings are accepted only as input aliases and are
# normalized to a stable relational key before validation or projection.
RELATION_ALIASES: dict[str, str] = {
    "hasInspection": "device_has_inspection",
    "hasDefect": "device_has_defect",
    "hasWorkOrder": "device_has_work_order",
    "hasTreatmentWorkOrder": "defect_has_work_order",
    "detectsDefect": "inspection_has_defect",
    "locatedAt": "device_located_at",
    "belongsToSite": "device_belongs_to_site",
    "managedByTeam": "device_managed_by_team",
    "generatedFrom": "defect_generated_from",
    "resolvedBy": "defect_resolved_by",
    "hasWorkPermit": "work_order_has_permit",
    "responsibleFor": "team_responsible_for_device",
    "parentOf": "device_parent_of",
    "parent_device": "device_parent_of",
    "sameFunctionLocation": "device_same_function_location",
    "same_function_location": "device_same_function_location",
    "related_device": "device_related_to_device",
    "relatedTo": "device_related_to_device",
    "hasSubject": "recorded_for",
    "governedByKnowledge": "governs_device_semantics",
    "evaluatedByKnowledge": "evaluates_device",
    "governedBy": "governed_by_rule",
    "derivedFrom": "derived_from_fact",
    "has_inspection": "device_has_inspection",
    "has_defect": "device_has_defect",
    "has_work_order": "device_has_work_order",
    "located_at": "device_located_at",
    "causes_defect": "inspection_causes_defect",
    "triggers_work_order": "defect_triggers_work_order",
    "resolves_defect": "work_order_resolves_defect",
}


EVENT_RELATION_REGISTRY: dict[str, str] = {
    "inspection_causes_defect": "causes",
    "defect_triggers_work_order": "triggers",
    "work_order_resolves_defect": "resolves",
    "inspection_records_defect": "records",
}


# Event type values are persisted in ``semantic_event.event_type`` and must be
# represented by concrete OWL classes.  The relational table is an index and
# is not allowed to invent an event class on its own.
EVENT_TYPE_CLASS_LOCAL_NAMES: dict[str, str] = {
    "InspectionEvent": "InspectionEvent",
    "AbnormalInspectionEvent": "AbnormalInspectionEvent",
    "DefectEvent": "DefectEvent",
    "DefectCreatedEvent": "DefectCreatedEvent",
    "DefectAcceptedEvent": "DefectAcceptedEvent",
    "DefectProcessingEvent": "DefectProcessingEvent",
    "ResolutionEvent": "ResolutionEvent",
    "DefectAcceptanceEvent": "DefectAcceptanceEvent",
    "WorkOrderEvent": "WorkOrderEvent",
    "WorkPermitEvent": "WorkPermitEvent",
    "HumanReviewEvent": "HumanReviewEvent",
    "BusinessEvent": "BusinessEvent",
}


# The ontology also contains object properties that are not direct entries in
# the relational relation registry (for example hasCurrentState).  Keeping
# the complete set here lets CI detect both missing and extra declarations.
ONTOLOGY_OBJECT_PROPERTY_LOCAL_NAMES: tuple[str, ...] = (
    "hasInspection", "hasDefect", "hasWorkOrder", "belongsToSite", "managedByTeam",
    "hasTreatmentWorkOrder", "generatedFrom", "resolvedBy", "hasWorkPermit", "detectsDefect",
    "locatedAt", "hasLocationAssignment", "assignmentDevice", "assignmentLocation",
    "relationSubject", "relationObject", "relationPredicate", "predicate", "hasSubject", "causes", "triggers",
    "resolves", "records", "hasEvent", "assertsIdentity", "hasFact", "hasCurrentState",
    "hasFactValue", "hasPrecondition", "requiresFact", "hasEffect", "hasExecutionState",
    "matchedByRule", "governedBy", "responsibleFor", "parentOf", "childDevice",
    "sameFunctionLocation", "relatedTo", "governedByKnowledge", "evaluatedByKnowledge",
    "derivedFrom", "derivedFromFact", "targetObjectType", "hasKnowledgeVersion", "hasFragment",
    "appliesToClass", "appliesToObject", "appliesToProperty", "relatedEvent", "relatedState",
    "relatedFact", "relatedRule", "relatedAction", "derivedFromKnowledge", "supportedBy",
    "approvedBy", "supersedes", "conflictsWith", "includedInRelease",
)


ONTOLOGY_DATATYPE_PROPERTY_LOCAL_NAMES: tuple[str, ...] = (
    "canonicalKey", "displayName", "sourceNamespace", "sourceSnapshotId", "sourceRecordId",
    "sourceSystem", "sourceSchema", "sourceTable", "confidence", "decisionMode", "predicateLabel",
    "correlationId", "activityType", "evidenceJson", "derivationId", "occurredAt", "recordedAt",
    "rawStatus", "description", "factType", "valueJson", "ruleVersion", "status", "validFrom",
    "validTo", "vocabularyVersion", "stateDomain", "isTerminal", "unit", "actionKey", "actionType",
    "targetKey", "reason", "riskLevel", "requiresApproval", "sourceWrite", "formalPublication",
    "actionName", "businessMeaning", "actionVersion", "allowedWhenJson", "requiredInputFactsJson",
    "permissionScopeJson", "adapterMappingsJson", "effectsJson", "executionStatesJson",
    "payloadJson", "valueText", "valueNumber", "valueBoolean", "valueDateTime", "conditionKey",
    "operator", "expectedValue", "requiredFactType", "requiredPredicate", "effectType", "effectTarget",
    "effectValue", "stateCode", "stateOrder", "packageId", "knowledgeDomain", "sourceType", "sourceId",
    "sourceUri", "owner", "reviewer", "contentHash", "knowledgeVersion", "extractionModel", "promptVersion", "pageNumber",
    "sectionPath", "qualityScore", "lifecycleStatus", "publishedAt", "deprecatedAt", "conflictType", "conflictStatus",
)


def canonical_object_type(value: object) -> str:
    return str(value or "business_object").strip().lower()


def object_class_local_name(value: object) -> str:
    key = canonical_object_type(value)
    return OBJECT_CLASS_LOCAL_NAMES.get(key, "BusinessObject")


def canonical_relation_key(value: object) -> str:
    key = str(value or "").strip()
    return RELATION_ALIASES.get(key, key)


def relation_predicate_local_name(value: object) -> str:
    key = canonical_relation_key(value)
    if key not in RELATION_REGISTRY:
        raise ValueError(f"未注册的业务关系谓词: {key}")
    return RELATION_REGISTRY[key][0]


def relation_domain_range(value: object) -> tuple[str, str]:
    key = canonical_relation_key(value)
    if key not in RELATION_REGISTRY:
        raise ValueError(f"未注册的业务关系类型: {key}")
    return RELATION_REGISTRY[key][1:]


def event_predicate_local_name(value: object) -> str:
    key = canonical_relation_key(value)
    if key not in EVENT_RELATION_REGISTRY:
        raise ValueError(f"未注册的事件关系谓词: {key}")
    return EVENT_RELATION_REGISTRY[key]


def validate_registry_contract() -> list[str]:
    """Return deterministic registry contract failures for CI and builders.

    The relational runtime accepts a small set of legacy aliases.  Every alias
    must resolve to exactly one registered relation or event predicate; this
    prevents a source row from silently becoming an untyped RDF relation.
    """
    failures: list[str] = []
    for key, (local_name, domain, value_range) in RELATION_REGISTRY.items():
        if not local_name or not domain or not value_range:
            failures.append(f"RELATION_CONTRACT_INCOMPLETE:{key}")
        for object_type in (domain, value_range):
            if object_type not in OBJECT_CLASS_LOCAL_NAMES:
                failures.append(f"RELATION_OBJECT_TYPE_UNREGISTERED:{key}:{object_type}")
    for alias, target in RELATION_ALIASES.items():
        if target not in RELATION_REGISTRY and target not in EVENT_RELATION_REGISTRY:
            failures.append(f"RELATION_ALIAS_TARGET_UNREGISTERED:{alias}:{target}")
    for storage_predicate in RELATIONAL_STORAGE_PREDICATES:
        target = RELATION_ALIASES.get(storage_predicate)
        if target is None:
            failures.append(f"RELATION_STORAGE_PREDICATE_ALIAS_MISSING:{storage_predicate}")
            continue
        if target not in RELATION_REGISTRY:
            failures.append(f"RELATION_STORAGE_PREDICATE_TARGET_UNREGISTERED:{storage_predicate}:{target}")
    for key, predicate in EVENT_RELATION_REGISTRY.items():
        if not key or not predicate:
            failures.append("EVENT_RELATION_CONTRACT_INCOMPLETE")
    failures.extend(validate_registry_against_ontology())
    return sorted(set(failures))


def validate_registry_against_ontology(ontology_path: object = None) -> list[str]:
    """Validate the bidirectional Registry ↔ OWL contract.

    The registry is the runtime lookup surface, while ``standards/ontology.ttl``
    is the semantic authority.  Neither side may silently grow a class or
    property that the other side cannot resolve.
    """
    try:
        from pathlib import Path

        from rdflib import Graph, URIRef
        from rdflib.namespace import OWL, RDF, RDFS, XSD
    except ImportError as exc:  # pragma: no cover - dependency gate owns this
        return [f"OWL_REGISTRY_VALIDATION_DEPENDENCY_MISSING:{exc}"]

    path = Path(ontology_path) if ontology_path else active_ontology_path()
    failures: list[str] = []
    graph = Graph()
    try:
        graph.parse(str(path), format="turtle")
    except Exception as exc:
        return [f"OWL_REGISTRY_ONTOLOGY_PARSE_FAILED:{exc}"]

    ontology_ns = "https://semantic.local/ontology/"

    def local_name(value: object) -> str:
        text = str(value)
        return text[len(ontology_ns):] if text.startswith(ontology_ns) else text.rsplit("/", 1)[-1].rsplit("#", 1)[-1]

    owl_classes = {
        local_name(item)
        for item in graph.subjects(RDF.type, OWL.Class)
        if str(item).startswith(ontology_ns)
    }
    registry_classes = set(OBJECT_CLASS_LOCAL_NAMES.values())
    for name in sorted(registry_classes - owl_classes):
        failures.append(f"OWL_CLASS_MISSING:{name}")
    for name in sorted(owl_classes - registry_classes):
        failures.append(f"REGISTRY_CLASS_MISSING:{name}")

    owl_object_properties = {
        local_name(item)
        for item in graph.subjects(RDF.type, OWL.ObjectProperty)
        if str(item).startswith(ontology_ns)
    }
    registry_object_properties = set(ONTOLOGY_OBJECT_PROPERTY_LOCAL_NAMES)
    for name in sorted(registry_object_properties - owl_object_properties):
        failures.append(f"OWL_OBJECT_PROPERTY_MISSING:{name}")
    for name in sorted(owl_object_properties - registry_object_properties):
        failures.append(f"REGISTRY_OBJECT_PROPERTY_MISSING:{name}")

    owl_datatype_properties = {
        local_name(item)
        for item in graph.subjects(RDF.type, OWL.DatatypeProperty)
        if str(item).startswith(ontology_ns)
    }
    registry_datatype_properties = set(ONTOLOGY_DATATYPE_PROPERTY_LOCAL_NAMES)
    for name in sorted(registry_datatype_properties - owl_datatype_properties):
        failures.append(f"OWL_DATATYPE_PROPERTY_MISSING:{name}")
    for name in sorted(owl_datatype_properties - registry_datatype_properties):
        failures.append(f"REGISTRY_DATATYPE_PROPERTY_MISSING:{name}")

    event_classes = set(EVENT_TYPE_CLASS_LOCAL_NAMES.values())
    for name in sorted(event_classes):
        iri = URIRef(ontology_ns + name)
        if (iri, RDF.type, OWL.Class) not in graph:
            failures.append(f"EVENT_CLASS_MISSING:{name}")
        if (iri, RDFS.subClassOf, URIRef(ontology_ns + "BusinessEvent")) not in graph and name != "BusinessEvent":
            failures.append(f"EVENT_CLASS_NOT_BUSINESS_EVENT:{name}")

    expected_domains_ranges = {
        "targetObjectType": (OWL.ObjectProperty, URIRef(ontology_ns + "BusinessObject"), OWL.Class),
        "validFrom": (OWL.DatatypeProperty, URIRef(ontology_ns + "BusinessObject"), XSD.dateTime),
        "validTo": (OWL.DatatypeProperty, URIRef(ontology_ns + "BusinessObject"), XSD.dateTime),
    }
    for name, (property_type, domain, value_range) in expected_domains_ranges.items():
        iri = URIRef(ontology_ns + name)
        if (iri, RDF.type, property_type) not in graph:
            failures.append(f"PROPERTY_TYPE_INVALID:{name}")
        if (iri, RDFS.domain, domain) not in graph:
            failures.append(f"PROPERTY_DOMAIN_INVALID:{name}")
        if (iri, RDFS.range, value_range) not in graph:
            failures.append(f"PROPERTY_RANGE_INVALID:{name}")

    return sorted(set(failures))


def active_ontology_path() -> Path:
    """Resolve the ontology asset behind the local release pointer."""
    standards = Path(__file__).resolve().parent.parent / "standards"
    root_spec_path = standards / "ontology-version.json"
    root_spec = json.loads(root_spec_path.read_text(encoding="utf-8"))
    registry_path = Path(__file__).resolve().parent / "data" / "ontology_version_registry.json"
    if registry_path.exists():
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
        active = str(registry.get("activeVersion") or "")
        if active and active != str(root_spec.get("version") or ""):
            candidate_spec = standards / f"ontology-version-{active.rsplit('/', 1)[-1]}.json"
            if candidate_spec.exists():
                candidate = json.loads(candidate_spec.read_text(encoding="utf-8"))
                return standards / str(candidate["ontologyFile"])
    return standards / str(root_spec.get("ontologyFile", "ontology.ttl"))
