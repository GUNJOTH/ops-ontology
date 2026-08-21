"""Registry tests for persisted relation spellings and RDF resolution."""
from __future__ import annotations

from semantic_registry import (
    RELATIONAL_STORAGE_PREDICATES,
    relation_predicate_local_name,
    validate_registry_contract,
)


def test_storage_predicates_are_registered_and_resolvable() -> None:
    assert validate_registry_contract() == []
    assert {
        predicate: relation_predicate_local_name(predicate)
        for predicate in RELATIONAL_STORAGE_PREDICATES
    } == {
        "parent_device": "parentOf",
        "same_function_location": "sameFunctionLocation",
        "related_device": "relatedTo",
    }


def test_same_function_location_snake_case_is_a_supported_alias() -> None:
    assert relation_predicate_local_name("same_function_location") == "sameFunctionLocation"
