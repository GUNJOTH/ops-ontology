"""Compatibility exports kept while callers migrate away from ``app.main``.

This module is intentionally a bridge only.  New code should import from the
owning domain, ``app.core`` or ``app.migrations`` directly.
"""
from __future__ import annotations

from app.bootstrap import build_app, lifespan
from app.core.db import (
    canonical_semantics_connection,
    duckdb_connection,
    identity_result_connection,
    latest_identity_result_db,
    metadata_sqlite_connection,
    sqlite_connection,
    unified_semantics_connection,
    unified_semantics_write_connection,
)
from app.domains.candidates.cluster_rules import ai_cluster_id, classify_ai_cluster, cluster_pattern
from app.domains.candidates.service import (
    agent_audit_pending_candidates,
    ai_auto_approve,
    candidate_detail,
    candidate_facets,
    candidates,
    formal_approval_queue,
    formal_batch_approve,
)
from app.domains.cleaning.service import register_cleaning_rule
from app.domains.decisions.router import (
    decision_layer_tables,
    review_semantic_action_plan,
    semantic_action_catalog,
    semantic_action_plan_detail,
    semantic_action_plans,
    semantic_action_plans_summary,
    semantic_decisions,
    semantic_decisions_summary,
)
from app.domains.knowledge_assets.router import (
    knowledge_asset_detail,
    knowledge_asset_summary,
    knowledge_assets,
)
from app.domains.knowledge_assets.service import knowledge_asset_row
from app.domains.locations.router import unified_location_detail, unified_location_summary, unified_locations
from app.domains.locations.service import unified_location_row
from app.domains.metadata.router import metadata_catalog, metadata_catalog_detail, metadata_export, metadata_summary
from app.domains.ontology.router import (
    ontology_event_types,
    ontology_meta_summary,
    ontology_object_types,
    ontology_relation_types,
)
from app.domains.ontology.service import ontology_meta_tables
from app.domains.semantic.router import (
    canonical_semantic_device,
    canonical_semantic_sparql,
    canonical_semantic_statements,
    canonical_semantic_summary,
    semantic_source_of_truth,
)
from app.domains.semantic_events.router import semantic_event_detail, semantic_events, semantic_events_summary
from app.domains.semantic_execution.router import (
    semantic_execution_dispatch,
    semantic_execution_preview,
    semantic_execution_summary,
)
from app.domains.semantic_facts.router import semantic_fact_detail, semantic_facts, semantic_facts_summary
from app.domains.semantic_status.router import (
    execute_status_mapping_replay_local,
    replay_semantic_states,
    replay_semantic_status_dictionary,
    review_semantic_status_dictionary,
    semantic_state_replay_diffs,
    semantic_states_summary,
    semantic_status_dictionary,
    semantic_status_dictionary_detail,
    semantic_status_dictionary_summary,
)
from app.domains.system.router import (
    health,
    semantic_metrics,
    semantic_release_activate,
    semantic_release_approve,
    semantic_release_backup,
    semantic_release_detail,
    semantic_release_rollback,
    semantic_releases,
)
from app.domains.unified_devices.router import unified_device_detail, unified_device_summary, unified_devices
from app.domains.unified_devices.service import unified_device_list_row
from app.domains.world_model.router import (
    decide_semantic_identity_review,
    revoke_semantic_identity,
    semantic_identity_review_detail,
    semantic_identity_review_queue,
    world_model_coverage,
    world_model_decision_explain,
    world_model_device_context,
    world_model_device_context_payload,
    world_model_device_timeline,
    world_model_fact_explain,
    world_model_governance_contract,
    world_model_runtime_contract,
    world_model_summary,
)
from app.migrations.workflow_steps import (
    ensure_cleaning_schema,
    ensure_review_sample_schema,
    initialize_workflow_schema,
)
from app.schemas.semantic import CanonicalSparqlRequest

__all__ = [
    "CanonicalSparqlRequest",
    "agent_audit_pending_candidates",
    "ai_cluster_id",
    "ai_auto_approve",
    "candidate_detail",
    "candidate_facets",
    "candidates",
    "canonical_semantics_connection",
    "canonical_semantic_device",
    "canonical_semantic_sparql",
    "canonical_semantic_statements",
    "canonical_semantic_summary",
    "classify_ai_cluster",
    "cluster_pattern",
    "decide_semantic_identity_review",
    "decision_layer_tables",
    "duckdb_connection",
    "ensure_cleaning_schema",
    "ensure_review_sample_schema",
    "execute_status_mapping_replay_local",
    "formal_approval_queue",
    "formal_batch_approve",
    "health",
    "identity_result_connection",
    "initialize_workflow_schema",
    "build_app",
    "lifespan",
    "knowledge_asset_detail",
    "knowledge_asset_row",
    "knowledge_assets",
    "knowledge_asset_summary",
    "latest_identity_result_db",
    "metadata_catalog",
    "metadata_catalog_detail",
    "metadata_export",
    "metadata_sqlite_connection",
    "metadata_summary",
    "ontology_event_types",
    "ontology_meta_summary",
    "ontology_meta_tables",
    "ontology_object_types",
    "ontology_relation_types",
    "register_cleaning_rule",
    "replay_semantic_states",
    "replay_semantic_status_dictionary",
    "review_semantic_action_plan",
    "review_semantic_status_dictionary",
    "revoke_semantic_identity",
    "semantic_action_catalog",
    "semantic_action_plan_detail",
    "semantic_action_plans",
    "semantic_action_plans_summary",
    "semantic_decisions",
    "semantic_decisions_summary",
    "semantic_event_detail",
    "semantic_events",
    "semantic_events_summary",
    "semantic_execution_dispatch",
    "semantic_execution_preview",
    "semantic_execution_summary",
    "semantic_fact_detail",
    "semantic_facts",
    "semantic_facts_summary",
    "semantic_identity_review_detail",
    "semantic_identity_review_queue",
    "semantic_metrics",
    "semantic_release_activate",
    "semantic_release_approve",
    "semantic_release_backup",
    "semantic_release_detail",
    "semantic_release_rollback",
    "semantic_releases",
    "semantic_source_of_truth",
    "semantic_state_replay_diffs",
    "semantic_states_summary",
    "semantic_status_dictionary",
    "semantic_status_dictionary_detail",
    "semantic_status_dictionary_summary",
    "sqlite_connection",
    "unified_device_detail",
    "unified_device_list_row",
    "unified_devices",
    "unified_device_summary",
    "unified_location_detail",
    "unified_location_row",
    "unified_locations",
    "unified_location_summary",
    "unified_semantics_connection",
    "unified_semantics_write_connection",
    "world_model_coverage",
    "world_model_decision_explain",
    "world_model_device_context",
    "world_model_device_context_payload",
    "world_model_device_timeline",
    "world_model_fact_explain",
    "world_model_governance_contract",
    "world_model_runtime_contract",
    "world_model_summary",
]
