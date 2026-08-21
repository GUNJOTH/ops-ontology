"""Domain projection stages for the Canonical RDF builder."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any

from build_canonical_semantic_model import (
    EX,
    PROV,
    SKOS,
    VOCABULARY_SCHEME_BY_TYPE,
    add_literal_properties,
    class_iri,
    datetime_literal,
    graph_iri,
    load_ontology,
    load_vocabulary,
    resource_iri,
    safe_segment,
    table_exists,
)
from rdflib import Literal, URIRef
from rdflib.namespace import RDF, XSD
from semantic_predicates import event_predicate, relation_predicate


def _json_items(raw: object) -> list[object]:
    """Normalize a JSON payload without losing the original payload literal."""
    if raw is None or str(raw).strip() == "":
        return []
    try:
        value = json.loads(str(raw))
    except (TypeError, json.JSONDecodeError):
        return []
    if isinstance(value, list):
        return value
    return [value]


def _semantic_node(parent: URIRef, category: str, index: int, payload: object) -> URIRef:
    digest = hashlib.sha256(
        (str(parent) + "|" + category + "|" + str(index) + "|" + json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)).encode("utf-8")
    ).hexdigest()[:24]
    return URIRef(EX + f"semantic/{category}/{digest}")


def _add_typed_value(builder, graph, node: URIRef, value: object, provenance: dict[str, Any]) -> None:
    if isinstance(value, bool):
        builder.add(graph, node, EX.valueBoolean, Literal(value, datatype=XSD.boolean), provenance=provenance)
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        builder.add(graph, node, EX.valueNumber, Literal(str(value), datatype=XSD.decimal), provenance=provenance)
    elif isinstance(value, str):
        builder.add(graph, node, EX.valueText, Literal(value, datatype=XSD.string), provenance=provenance)


def _add_json_semantic_node(builder, graph, parent: URIRef, category: str, index: int, payload: object, node_class: URIRef, link: URIRef, provenance: dict[str, Any]) -> URIRef:
    node = _semantic_node(parent, category, index, payload)
    builder.add(graph, parent, link, node, provenance=provenance)
    builder.add(graph, node, RDF.type, node_class, provenance=provenance)
    builder.add(graph, node, EX.payloadJson, Literal(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str), datatype=XSD.string), provenance=provenance)
    return node


def _add_fact_value_semantics(builder, graph, fact: URIRef, raw: object, provenance: dict[str, Any]) -> None:
    for index, payload in enumerate(_json_items(raw)):
        node = _add_json_semantic_node(builder, graph, fact, "fact-value", index, payload, EX.FactValue, EX.hasFactValue, provenance)
        if isinstance(payload, dict):
            value = payload.get("value", payload.get("amount", payload.get("text")))
        else:
            value = payload
        _add_typed_value(builder, graph, node, value, provenance)


def _add_action_semantics(builder, graph, action: URIRef, row: sqlite3.Row) -> None:
    provenance = dict(row)
    for category, raw, node_class, link in (
        ("action-condition", row["allowed_when_json"], EX.ActionCondition, EX.hasPrecondition),
        ("required-fact", row["required_facts_json"], EX.RequiredFact, EX.requiresFact),
        ("action-effect", row["effects_json"], EX.ActionEffect, EX.hasEffect),
        ("execution-state", row["execution_states_json"], EX.ExecutionState, EX.hasExecutionState),
    ):
        for index, payload in enumerate(_json_items(raw)):
            node = _add_json_semantic_node(builder, graph, action, category, index, payload, node_class, link, provenance)
            if isinstance(payload, dict):
                if category == "action-condition":
                    for predicate, key in ((EX.conditionKey, "key"), (EX.operator, "operator"), (EX.expectedValue, "value")):
                        if payload.get(key) is not None:
                            builder.add(graph, node, predicate, Literal(str(payload[key]), datatype=XSD.string), provenance=provenance)
                elif category == "required-fact":
                    for predicate, keys in ((EX.requiredFactType, ("factType", "fact_type", "type")), (EX.requiredPredicate, ("predicate", "predicateLabel"))):
                        value = next((payload.get(key) for key in keys if payload.get(key) is not None), None)
                        if value is not None:
                            builder.add(graph, node, predicate, Literal(str(value), datatype=XSD.string), provenance=provenance)
                elif category == "action-effect":
                    for predicate, keys in ((EX.effectType, ("effectType", "type")), (EX.effectTarget, ("target", "targetKey")), (EX.effectValue, ("value", "result"))):
                        value = next((payload.get(key) for key in keys if payload.get(key) is not None), None)
                        if value is not None:
                            builder.add(graph, node, predicate, Literal(str(value), datatype=XSD.string), provenance=provenance)
            elif category == "execution-state":
                builder.add(graph, node, EX.stateCode, Literal(str(payload), datatype=XSD.string), provenance=provenance)
            if category == "execution-state":
                builder.add(graph, node, EX.stateOrder, Literal(index, datatype=XSD.integer), provenance=provenance)


def build_ontology(builder, source_db: sqlite3.Connection) -> None:
    graph = builder.dataset.graph(URIRef(graph_iri("ontology", builder.run_id, ontology_version=builder.ontology_version)))
    for triple in load_ontology():
        graph.add(triple)

    static_vocabulary = load_vocabulary()
    for triple in static_vocabulary:
        graph.add(triple)
        builder.vocabulary_counts["staticStatements"] += 1
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
            builder.vocabulary_counts[bucket] += 1

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
        builder.add(graph, concept, RDF.type, SKOS.Concept, provenance=provenance)
        builder.add(graph, concept, SKOS.inScheme, scheme, provenance=provenance)
        add_literal_properties(builder.dataset, graph, concept, [
            (SKOS.notation, notation, XSD.string),
            (SKOS.prefLabel, label, None),
            (SKOS.definition, definition, None),
            (SKOS.altLabel, alternative, None),
            (EX.vocabularyVersion, "operations-vocabulary-v1", XSD.string),
        ])
        builder.vocabulary_counts[category] += 1

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
            builder.add(graph, concept, RDF.type, EX.CanonicalState, provenance=dict(row))
            add_literal_properties(builder.dataset, graph, concept, [
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
                builder.add(graph, concept, SKOS.exactMatch, URIRef(EX + "state/DEFECT/" + safe_segment(row["canonical_state"])), provenance=dict(row))
            add_literal_properties(builder.dataset, graph, concept, [
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

def build_objects(builder, source_db: sqlite3.Connection) -> None:
    graph = builder.source_graph(None)
    rows = source_db.execute("SELECT * FROM semantic_object_instance WHERE status IN ('accepted','observed','derived','current') ORDER BY object_type,object_id").fetchall()
    for row in rows:
        object_type = str(row["object_type"] or "business_object").lower()
        key = str(row["canonical_key"])
        iri = URIRef(EX + f"object/{safe_segment(object_type)}/{safe_segment(key)}")
        builder.object_index[(object_type, key)] = iri
        builder.add(graph, iri, RDF.type, class_iri(object_type), status=str(row["status"]))
        add_literal_properties(builder.dataset, graph, iri, [
            (EX.canonicalKey, row["canonical_key"], XSD.string),
            (EX.displayName, row["display_name"], XSD.string),
            (EX.sourceNamespace, row["source_namespace"], XSD.string),
            (EX.sourceSnapshotId, row["source_snapshot_id"], XSD.string),
            (EX.status, row["status"], XSD.string),
        ])
        builder.add_provenance("object_projection", builder.run_id, iri, source_system=row["source_namespace"], source_table="semantic_object_instance", source_row_id=row["object_id"], source_snapshot_id=row["source_snapshot_id"], evidence=row["evidence_json"])

def build_identity_assertions(builder, source_db: sqlite3.Connection) -> None:
    graph = builder.source_graph(None)
    rows = source_db.execute("SELECT * FROM semantic_identity_assertion WHERE status='accepted' AND lifecycle_status='active' ORDER BY assertion_id").fetchall()
    for row in rows:
        identity = URIRef(EX + f"identity/{safe_segment(row['assertion_id'])}")
        target = resource_iri(row["canonical_object_type"], row["canonical_object_id"], builder.object_index)
        builder.add(graph, identity, RDF.type, EX.IdentityAssertion, confidence=row["confidence"], provenance=dict(row))
        builder.add(graph, identity, EX.assertsIdentity, target, confidence=row["confidence"], provenance=dict(row))
        add_literal_properties(builder.dataset, graph, identity, [
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
        builder.add_provenance("identity_assertion", row["assertion_id"], identity, source_system=row["source_system"], source_schema=row["source_schema"], source_table=row["source_table"], source_row_id=row["source_row_id"], source_snapshot_id=row["source_snapshot_id"], evidence=row["evidence_json"])

def build_relations(builder, source_db: sqlite3.Connection) -> None:
    graph = builder.derived_graph
    rows = source_db.execute("SELECT * FROM semantic_relation_assertion WHERE status='accepted' ORDER BY relation_id").fetchall()
    for row in rows:
        subject = resource_iri(row["subject_type"], row["subject_key"], builder.object_index)
        obj = resource_iri(row["object_type"], row["object_key"], builder.object_index)
        predicate = URIRef(EX + relation_predicate(row["relation_type"]))
        builder.add(graph, subject, predicate, obj, confidence=row["confidence"], provenance=dict(row))
        relation_class = EX.LocationAssignment if str(row["object_type"]).lower() == "location" else EX.RelationAssertion
        if row["valid_from"] or row["valid_to"] or relation_class == EX.LocationAssignment:
            assertion = URIRef(EX + "relation/" + safe_segment(row["relation_id"]))
            builder.add(graph, assertion, RDF.type, relation_class, confidence=row["confidence"], provenance=dict(row))
            builder.add(graph, assertion, EX.relationSubject, subject, confidence=row["confidence"], provenance=dict(row))
            builder.add(graph, assertion, EX.relationObject, obj, confidence=row["confidence"], provenance=dict(row))
            builder.add(graph, assertion, EX.relationPredicate, predicate, confidence=row["confidence"], provenance=dict(row))
            add_literal_properties(builder.dataset, graph, assertion, [
                (EX.validFrom, datetime_literal(row["valid_from"]), None),
                (EX.validTo, datetime_literal(row["valid_to"]), None),
            ])
            if relation_class == EX.LocationAssignment:
                builder.add(graph, subject, EX.hasLocationAssignment, assertion, confidence=row["confidence"], provenance=dict(row))
                builder.add(graph, assertion, EX.assignmentDevice, subject, confidence=row["confidence"], provenance=dict(row))
                builder.add(graph, assertion, EX.assignmentLocation, obj, confidence=row["confidence"], provenance=dict(row))
        builder.add_provenance("relation_assertion", row["relation_id"], subject, source_system=row["source_schema"], source_schema=row["source_schema"], source_table=row["source_table"], source_row_id=row["source_row_id"], source_snapshot_id=row["source_snapshot_id"], evidence=row["evidence_json"])

def build_events(builder, source_db: sqlite3.Connection) -> None:
    graph = builder.source_graph(None)
    rows = source_db.execute("SELECT * FROM semantic_event WHERE status IN ('observed','accepted') ORDER BY event_id").fetchall()
    event_index: dict[str, URIRef] = {}
    for row in rows:
        event = URIRef(EX + f"event/{safe_segment(row['event_id'])}")
        event_index[str(row["event_id"])] = event
        event_type = str(row["event_type"] or "BusinessEvent")
        builder.add(graph, event, RDF.type, EX.BusinessEvent, confidence=row["confidence"], provenance=dict(row))
        builder.add(graph, event, RDF.type, class_iri(event_type), confidence=row["confidence"], provenance=dict(row))
        builder.add(graph, event, RDF.type, URIRef(EX + safe_segment(event_type)), confidence=row["confidence"], provenance=dict(row))
        subject = resource_iri(row["subject_type"], row["subject_key"], builder.object_index)
        builder.add(graph, event, EX.hasSubject, subject, confidence=row["confidence"], provenance=dict(row))
        add_literal_properties(builder.dataset, graph, event, [
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
        builder.add_provenance("event_projection", row["event_id"], event, source_system=row["source_schema"], source_schema=row["source_schema"], source_table=row["source_table"], source_row_id=row["source_row_id"], source_snapshot_id=row["source_snapshot_id"], evidence=row["payload_json"])
    relation_rows = source_db.execute("SELECT * FROM semantic_event_relation WHERE status='accepted' ORDER BY relation_id").fetchall()
    for row in relation_rows:
        subject = event_index.get(str(row["subject_event_id"]))
        obj = event_index.get(str(row["object_event_id"]))
        if subject is None or obj is None:
            continue
        predicate = URIRef(EX + event_predicate(row["relation_type"]))
        builder.add(builder.derived_graph, subject, predicate, obj, confidence=row["confidence"], provenance=dict(row))
        builder.add_provenance("event_relation", row["relation_id"], subject, source_system=row["source_schema"], source_schema=row["source_schema"], source_table=row["source_table"], source_row_id=row["source_row_id"], source_snapshot_id=row["source_snapshot_id"], evidence=row["evidence_json"])

def build_facts_states_rules(builder, source_db: sqlite3.Connection) -> None:
    graph = builder.derived_graph
    for row in source_db.execute("SELECT * FROM semantic_fact WHERE status IN ('observed','accepted','derived') ORDER BY fact_id"):
        fact = URIRef(EX + f"fact/{safe_segment(row['fact_id'])}")
        subject = resource_iri(row["subject_type"], row["subject_key"], builder.object_index)
        builder.add(graph, fact, RDF.type, EX.Fact, confidence=row["confidence"], provenance=dict(row))
        fact_class = EX.DerivedFact if str(row["status"]) == "derived" else EX.SourceFact
        builder.add(graph, fact, RDF.type, fact_class, confidence=row["confidence"], provenance=dict(row))
        builder.add(graph, subject, EX.hasFact, fact, confidence=row["confidence"], provenance=dict(row))
        predicate_label = str(row["predicate"] or "relatedTo")
        predicate_iri = URIRef(EX + "predicate/" + safe_segment(predicate_label))
        builder.add(graph, predicate_iri, RDF.type, URIRef("http://www.w3.org/1999/02/22-rdf-syntax-ns#Property"), provenance=dict(row))
        builder.add(graph, fact, EX.predicate, predicate_iri, provenance=dict(row))
        add_literal_properties(builder.dataset, graph, fact, [
            (EX.factType, row["fact_type"], XSD.string),
            (EX.predicateLabel, predicate_label, XSD.string),
            (EX.valueJson, row["value_json"], XSD.string),
            (EX.unit, row["unit"], XSD.string),
            (EX.sourceSchema, row["source_schema"], XSD.string),
            (EX.sourceTable, row["source_table"], XSD.string),
            (EX.sourceRecordId, row["source_row_id"], XSD.string),
            (EX.sourceSnapshotId, row["source_snapshot_id"], XSD.string),
        ])
        _add_fact_value_semantics(builder, graph, fact, row["value_json"], dict(row))
        if str(row["status"]) == "derived" and table_exists(source_db, "semantic_fact_derivation"):
            derivations = source_db.execute(
                "SELECT * FROM semantic_fact_derivation WHERE output_fact_id=? AND status='accepted' ORDER BY derivation_id",
                (row["fact_id"],),
            ).fetchall()
            for derivation in derivations:
                activity = URIRef(EX + "derivation/" + safe_segment(derivation["derivation_id"]))
                builder.add(graph, activity, RDF.type, PROV.Activity, provenance=dict(derivation))
                builder.add(graph, fact, PROV.wasGeneratedBy, activity, provenance=dict(derivation))
                builder.add(graph, activity, EX.derivationId, Literal(str(derivation["derivation_id"]), datatype=XSD.string), provenance=dict(derivation))
                try:
                    input_fact_ids = json.loads(derivation["input_fact_ids_json"] or "[]")
                except (TypeError, json.JSONDecodeError):
                    input_fact_ids = []
                if isinstance(input_fact_ids, list):
                    for input_fact_id in input_fact_ids:
                        input_fact = URIRef(EX + "fact/" + safe_segment(input_fact_id))
                        builder.add(graph, fact, EX.derivedFromFact, input_fact, provenance=dict(derivation))
        builder.add_provenance("fact_projection", row["fact_id"], fact, source_system=row["source_schema"], source_schema=row["source_schema"], source_table=row["source_table"], source_row_id=row["source_row_id"], source_snapshot_id=row["source_snapshot_id"], evidence=row["value_json"])
    for row in source_db.execute("SELECT * FROM semantic_current_state WHERE status='current' ORDER BY subject_key,state_domain"):
        subject = resource_iri(row["subject_type"], row["subject_key"], builder.object_index)
        state = URIRef(EX + "state/" + safe_segment(row["state_domain"]) + "/" + safe_segment(row["current_state"]))
        builder.add(graph, state, RDF.type, EX.CanonicalState, provenance=dict(row))
        builder.add(graph, subject, EX.hasCurrentState, state, provenance=dict(row))
    for row in source_db.execute("SELECT * FROM semantic_executable_rule WHERE status IN ('replayed','approved','enabled') ORDER BY rule_id,rule_version"):
        rule = URIRef(EX + f"rule/{safe_segment(row['rule_id'])}/{safe_segment(row['rule_version'])}")
        builder.add(graph, rule, RDF.type, EX.Rule, provenance=dict(row))
        add_literal_properties(builder.dataset, graph, rule, [
            (EX.ruleVersion, row["rule_version"], XSD.string),
            (EX.displayName, row["title"], XSD.string),
            (EX.status, row["status"], XSD.string),
            (EX.sourceSnapshotId, row["source_version_id"], XSD.string),
        ])
        builder.add(graph, rule, EX.targetObjectType, class_iri(row["target_object_type"]), provenance=dict(row))
        builder.add_provenance("rule_projection", row["rule_id"], rule, source_system="local", source_table="semantic_executable_rule", source_row_id=row["rule_id"], source_snapshot_id=row["source_version_id"], evidence=row["provenance_json"])
    has_action_definition = source_db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='semantic_action_definition'").fetchone() is not None
    if has_action_definition:
        for row in source_db.execute("SELECT * FROM semantic_action_definition ORDER BY action_id"):
            action = URIRef(EX + "action/" + safe_segment(row["action_id"]))
            builder.add(graph, action, RDF.type, EX.Action, provenance=dict(row))
            add_literal_properties(builder.dataset, graph, action, [
                (EX.actionName, row["action_name"], XSD.string),
                (EX.businessMeaning, row["business_meaning"], XSD.string),
                (EX.actionVersion, row["version"], XSD.string),
                (EX.allowedWhenJson, row["allowed_when_json"], XSD.string),
                (EX.requiredInputFactsJson, row["required_facts_json"], XSD.string),
                (EX.permissionScopeJson, row["permission_scope_json"], XSD.string),
                (EX.adapterMappingsJson, row["adapter_mappings_json"], XSD.string),
                (EX.effectsJson, row["effects_json"], XSD.string),
                (EX.executionStatesJson, row["execution_states_json"], XSD.string),
                (EX.status, row["status"], XSD.string),
            ])
            _add_action_semantics(builder, graph, action, row)
            builder.add(graph, action, EX.targetObjectType, class_iri(row["target_type"]), provenance=dict(row))
            builder.add_provenance("action_definition_projection", row["action_id"], action, source_system="local", source_table="semantic_action_definition", source_row_id=row["action_id"], evidence=row["business_meaning"])
    has_action_plan = source_db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='semantic_action_plan'").fetchone() is not None
    if has_action_plan:
        for row in source_db.execute("SELECT * FROM semantic_action_plan ORDER BY plan_id"):
            plan = URIRef(EX + "action-plan/" + safe_segment(row["plan_id"]))
            builder.add(graph, plan, RDF.type, EX.ActionPlan, provenance=dict(row))
            add_literal_properties(builder.dataset, graph, plan, [
                (EX.actionKey, row["action_key"], XSD.string),
                (EX.actionType, row["action_type"], XSD.string),
                (EX.targetKey, row["target_key"], XSD.string),
                (EX.reason, row["reason"], XSD.string),
                (EX.riskLevel, row["risk_level"], XSD.string),
                (EX.requiresApproval, bool(row["requires_approval"]), XSD.boolean),
                (EX.sourceWrite, bool(row["source_write"]), XSD.boolean),
                (EX.formalPublication, bool(row["formal_publication"]), XSD.boolean),
                (EX.status, row["status"], XSD.string),
            ])
            builder.add(graph, plan, EX.targetObjectType, class_iri(row["target_type"]), provenance=dict(row))
            builder.add_provenance("action_plan_projection", row["plan_id"], plan, source_system="local", source_table="semantic_action_plan", source_row_id=row["plan_id"], evidence=row["payload_json"])
