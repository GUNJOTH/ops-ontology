"""World-model device context and timeline services."""
from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Query

from app.core.canonical_reader import CanonicalReader
from app.core.db import canonical_semantics_connection, identity_result_connection, unified_semantics_connection
from app.core.utils import ontology_trace_id, utc_now
from app.domains.ontology.service import ontology_meta_tables
from app.domains.unified_devices.service import unified_device_list_row


def world_model_device_context_payload(unified_device_id: str, as_of: str | None = None) -> dict[str, Any]:
    identity = identity_result_connection()
    overlay = unified_semantics_connection()
    canonical_connection = canonical_semantics_connection()
    try:
        device = identity.execute("SELECT * FROM unified_device WHERE unified_device_id=?", (unified_device_id,)).fetchone()
        if device is None:
            raise HTTPException(status_code=404, detail="统一设备对象不存在")
        canonical_context = CanonicalReader(canonical_connection).device_context(unified_device_id, as_of)
        mappings = [dict(row) for row in identity.execute(
            """SELECT source_schema,source_table_group,source_table,source_row_id,source_key_type,source_key,
               raw_description,location_code,match_method,match_confidence,status,evidence_json,source_snapshot_id
               FROM device_identity_map WHERE unified_device_id=? ORDER BY source_schema,source_table,source_row_id""",
            (unified_device_id,),
        ).fetchall()]
        # Canonical RDF is the read authority wherever the object has already
        # entered the completed projection. Until the migration coverage gate
        # reaches 100%, an unprojected object is served by the read-only
        # relational compatibility path and is explicitly labelled pending.
        if canonical_context is not None:
            relation_rows = canonical_context["relations"]
            recent_events = canonical_context["events"]
            facts = canonical_context["facts"]
            current_states = canonical_context["states"]
        else:
            relation_rows = [dict(row) for row in overlay.execute(
                """SELECT relation_id,subject_type,subject_key,predicate,object_type,object_key,source_schema,source_table,
                   source_row_id,status,confidence,evidence_json,source_snapshot_id
                   FROM business_object_relation WHERE (subject_type='device' AND subject_key=?)
                      OR (object_type='device' AND object_key=?) ORDER BY predicate,relation_id""",
                (unified_device_id, unified_device_id),
            ).fetchall()]
            event_where = "subject_key=?"
            event_params: list[Any] = [unified_device_id]
            if as_of:
                event_where += " AND (occurred_at IS NULL OR occurred_at<=?)"
                event_params.append(as_of)
            recent_events = [dict(row) for row in overlay.execute(
                f"SELECT * FROM semantic_event WHERE {event_where} AND status IN ('observed','accepted') ORDER BY occurred_at DESC,event_id LIMIT 200",
                event_params,
            ).fetchall()]
            fact_where = "subject_key=? AND status<>'retracted'"
            fact_params: list[Any] = [unified_device_id]
            if as_of:
                fact_where += " AND (observed_at IS NULL OR observed_at<=?)"
                fact_params.append(as_of)
            facts = [dict(row) for row in overlay.execute(
                f"SELECT * FROM semantic_fact WHERE {fact_where} ORDER BY created_at DESC,fact_id LIMIT 500",
                fact_params,
            ).fetchall()]
            current_states = [dict(row) for row in overlay.execute(
                "SELECT * FROM semantic_current_state WHERE subject_key=? AND status='current' ORDER BY state_domain",
                (unified_device_id,),
            ).fetchall()]
        decisions = [dict(row) for row in overlay.execute(
            "SELECT * FROM semantic_rule_decision WHERE subject_key=? AND status IN ('accepted','proposed','needs_review') ORDER BY created_at DESC LIMIT 200",
            (unified_device_id,),
        ).fetchall()]
        decision_ids = [row["decision_id"] for row in decisions]
        actions: list[dict[str, Any]] = []
        if decision_ids and "semantic_action_plan" in ontology_meta_tables(overlay):
            marks = ",".join("?" for _ in decision_ids)
            actions = [dict(row) for row in overlay.execute(
                f"SELECT * FROM semantic_action_plan WHERE decision_id IN ({marks}) ORDER BY updated_at DESC",
                decision_ids,
            ).fetchall()]
        rules = [dict(row) for row in overlay.execute(
            "SELECT * FROM semantic_logic_rule WHERE status='enabled' ORDER BY rule_asset_id"
        ).fetchall()]
        if "semantic_action_rule" in ontology_meta_tables(overlay):
            rules.extend(dict(row) for row in overlay.execute(
                """SELECT rule_id AS rule_asset_id,rule_version AS rule_version_id,title AS decision,action_type AS output_fact_type,
                   risk_level,requires_approval,status,source_write,formal_publication
                   FROM semantic_action_rule WHERE status='enabled' ORDER BY rule_id"""
            ).fetchall())
        observed = sum(1 for row in facts if row["status"] == "observed")
        derived = sum(1 for row in facts if row["status"] in {"derived", "accepted"})
        object_payload = unified_device_list_row(
            device,
            len(mappings),
            len(mappings),
            len(relation_rows),
            len(mappings),
            0,
        )
        return {
            "schemaVersion": "world-context-v1",
            "semanticSourceOfTruth": "canonical-rdf-dataset" if canonical_context is not None else "relational-runtime-pending-canonical",
            "canonicalRunId": canonical_context["runId"] if canonical_context is not None else None,
            "canonicalSubjectIri": canonical_context["subjectIri"] if canonical_context is not None else None,
            "object": object_payload,
            "identity": {"sourceIdentityKey": device["source_identity_key"], "mappings": mappings},
            "organization": {"siteId": device["site_id"], "orgId": device["org_id"] or "", "classificationId": device["classification_id"] or ""},
            "relations": relation_rows,
            "currentStates": current_states,
            "recentEvents": recent_events,
            "observedFacts": [row for row in facts if row["status"] == "observed"],
            "derivedFacts": [row for row in facts if row["status"] in {"derived", "accepted"}],
            "applicableRules": rules,
            "decisions": decisions,
            "pendingActions": actions,
            "evidenceSummary": {"identityMappings": len(mappings), "relationCount": len(relation_rows), "eventCount": len(recent_events), "observedFactCount": observed, "derivedFactCount": derived, "canonicalProvenance": canonical_context is not None},
            "asOf": as_of or utc_now(),
            "traceId": ontology_trace_id("WORLD", unified_device_id),
            "sourceWrite": False,
            "formalPublication": False,
        }
    finally:
        canonical_connection.close()
        overlay.close()
        identity.close()

def world_model_device_context(unified_device_id: str, as_of: str | None = None) -> dict[str, Any]:
    return world_model_device_context_payload(unified_device_id, as_of)

def world_model_device_timeline(
    unified_device_id: str,
    from_time: str | None = Query(default=None, alias="from"),
    to_time: str | None = Query(default=None, alias="to"),
    event_type: str = "all",
) -> dict[str, Any]:
    context = world_model_device_context_payload(unified_device_id, to_time)
    events = context["recentEvents"]
    if event_type != "all":
        events = [row for row in events if row.get("event_type") == event_type]
    if from_time:
        events = [row for row in events if not row.get("occurred_at") or row.get("occurred_at") >= from_time]
    transitions: list[dict[str, Any]] = []
    connection = unified_semantics_connection()
    try:
        transitions = [dict(row) for row in connection.execute(
            "SELECT * FROM semantic_state_transition WHERE subject_key=? ORDER BY effective_at DESC,created_at DESC",
            (unified_device_id,),
        ).fetchall()]
    finally:
        connection.close()
    timeline = [
        {"kind": "event", "at": row.get("occurred_at") or row.get("recorded_at"), "id": row.get("event_id"), "payload": row}
        for row in events
    ] + [
        {"kind": "state_transition", "at": row.get("effective_at") or row.get("created_at"), "id": row.get("transition_id"), "payload": row}
        for row in transitions
    ]
    timeline.sort(key=lambda row: (row.get("at") or "", row.get("id") or ""), reverse=True)
    return {"schemaVersion": "world-timeline-v1", "unifiedDeviceId": unified_device_id, "items": timeline, "currentStates": context["currentStates"], "traceId": ontology_trace_id("TIMELINE", unified_device_id), "sourceWrite": False, "formalPublication": False}
