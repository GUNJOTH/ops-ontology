"""Single semantic vocabulary registry for the relational and RDF layers.

The local SQLite tables keep stable, readable relation keys.  The canonical
RDF model uses the explicitly registered local names below.  Keeping this
mapping in one module prevents business_object_relation, ontology metadata,
semantic_relation_contract and the RDF projection from silently inventing
different predicates or object classes.
"""
from __future__ import annotations


OBJECT_CLASS_LOCAL_NAMES: dict[str, str] = {
    "business_object": "BusinessObject",
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
    "abnormal_inspection": "AbnormalInspection",
    "defect": "Defect",
    "defect_event": "BusinessEvent",
    "defect_resolution": "DefectResolution",
    "resolution_event": "BusinessEvent",
    "work_order": "WorkOrder",
    "work_order_event": "BusinessEvent",
    "work_permit": "WorkPermit",
    "human_review": "HumanReview",
    "observation_event": "BusinessEvent",
    "escalation_event": "BusinessEvent",
    "business_event": "BusinessEvent",
    "business_knowledge": "BusinessKnowledge",
    "knowledge_asset": "KnowledgeAsset",
    "standard": "Standard",
    "rule": "Rule",
    "sop": "SOP",
    "risk": "Risk",
    "rule_decision": "RuleDecision",
    "semantic_fact": "Fact",
    "fact": "Fact",
    "action_plan": "ActionPlan",
    "repeated_defect": "RepeatedDefect",
    "severe_defect": "SevereDefect",
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
    return sorted(set(failures))
