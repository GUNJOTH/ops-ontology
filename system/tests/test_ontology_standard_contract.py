"""Regression tests for the OWL-first semantic contracts."""
from __future__ import annotations

from pathlib import Path

from rdflib import Graph, Literal, URIRef
from rdflib.namespace import OWL, RDF, RDFS, XSD
from replay_owl_rl import apply_rules
from semantic_namespaces import ONTOLOGY_NAMESPACE
from semantic_registry import validate_registry_against_ontology

ROOT = Path(__file__).resolve().parents[2]
ONTOLOGY_PATH = ROOT / "standards" / "ontology.ttl"
EX = ONTOLOGY_NAMESPACE


def ontology_graph() -> Graph:
    graph = Graph()
    graph.parse(str(ONTOLOGY_PATH), format="turtle")
    return graph


def test_registry_and_ontology_are_bidirectionally_consistent() -> None:
    assert validate_registry_against_ontology() == []


def test_owl_first_class_hierarchy_has_one_authoritative_parent() -> None:
    graph = ontology_graph()
    mapped_classes = {
        str(subject)
        for subject in graph.subjects(RDF.type, OWL.Class)
        if str(subject).startswith(EX)
    }
    assert mapped_classes
    for class_iri in mapped_classes:
        parents = {
            parent
            for parent in graph.objects(URIRef(class_iri), RDFS.subClassOf)
            if isinstance(parent, URIRef) and str(parent).startswith(EX)
        }
        assert len(parents) <= 1, f"多个本体父类会让关系型投影失去唯一层级: {class_iri}"


def test_target_object_type_and_assertion_time_properties_have_correct_contracts() -> None:
    graph = ontology_graph()
    target = URIRef(EX + "targetObjectType")
    valid_from = URIRef(EX + "validFrom")
    valid_to = URIRef(EX + "validTo")
    assert (target, RDF.type, OWL.ObjectProperty) in graph
    assert (target, RDFS.domain, URIRef(EX + "BusinessObject")) in graph
    assert (target, RDFS.range, OWL.Class) in graph
    for predicate in (valid_from, valid_to):
        assert (predicate, RDF.type, OWL.DatatypeProperty) in graph
        assert (predicate, RDFS.domain, URIRef(EX + "Assertion")) in graph
        assert (predicate, RDFS.range, XSD.dateTime) in graph


def test_internal_rl_replay_does_not_create_forbidden_cross_class_inferences() -> None:
    graph = ontology_graph()
    action = URIRef(EX + "resource/action/test")
    assertion = URIRef(EX + "resource/assertion/test")
    graph.add((action, RDF.type, URIRef(EX + "Action")))
    graph.add((action, URIRef(EX + "targetObjectType"), URIRef(EX + "Device")))
    graph.add((assertion, RDF.type, URIRef(EX + "IdentityAssertion")))
    graph.add((assertion, URIRef(EX + "validFrom"), Literal("2026-08-21T00:00:00+00:00", datatype=XSD.dateTime)))

    inferred, _support, _iterations = apply_rules(graph)
    assert (action, RDF.type, URIRef(EX + "Rule")) not in inferred
    assert (assertion, RDF.type, URIRef(EX + "RelationAssertion")) not in inferred


def test_event_types_are_formal_business_event_classes() -> None:
    graph = ontology_graph()
    event_types = (
        "InspectionEvent",
        "AbnormalInspectionEvent",
        "DefectEvent",
        "DefectCreatedEvent",
        "DefectAcceptedEvent",
        "DefectProcessingEvent",
        "ResolutionEvent",
        "DefectAcceptanceEvent",
        "WorkOrderEvent",
        "WorkPermitEvent",
        "HumanReviewEvent",
    )
    for event_type in event_types:
        event_iri = URIRef(EX + event_type)
        assert (event_iri, RDF.type, OWL.Class) in graph
        assert (event_iri, RDFS.subClassOf, URIRef(EX + "BusinessEvent")) in graph
