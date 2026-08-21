"""Compatibility facade for the single semantic vocabulary registry."""
from __future__ import annotations

from semantic_registry import (
    EVENT_RELATION_REGISTRY,
    RELATION_REGISTRY,
    canonical_relation_key,
    event_predicate_local_name,
    relation_predicate_local_name,
)

RELATION_PREDICATES = {key: value[0] for key, value in RELATION_REGISTRY.items()}
EVENT_PREDICATES = dict(EVENT_RELATION_REGISTRY)


def relation_predicate(relation_type: object) -> str:
    return relation_predicate_local_name(canonical_relation_key(relation_type))


def event_predicate(relation_type: object) -> str:
    return event_predicate_local_name(canonical_relation_key(relation_type))
