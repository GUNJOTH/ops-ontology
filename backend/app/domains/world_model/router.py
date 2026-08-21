"""World model query and review routes (native APIRouter)."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query

from app.core.auth import require_decision_auth
from app.core.canonical_reader import CanonicalReader
from app.core.db import (
    canonical_semantics_connection,
    identity_result_connection,
    unified_semantics_connection,
    unified_semantics_write_connection,
)
from app.core.utils import ontology_trace_id, parse_json_array, utc_now
from app.domains.ontology.service import ontology_meta_tables
from app.domains.unified_devices.service import unified_device_list_row
from app.schemas.semantic import SemanticIdentityReviewRequest, SemanticIdentityRevokeRequest


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


def world_model_fact_explain(fact_id: str) -> dict[str, Any]:
    connection = unified_semantics_connection()
    try:
        fact = connection.execute("SELECT * FROM semantic_fact WHERE fact_id=?", (fact_id,)).fetchone()
        if fact is None:
            raise HTTPException(status_code=404, detail="语义事实不存在")
        derivations = [dict(row) for row in connection.execute(
            "SELECT * FROM semantic_fact_derivation WHERE output_fact_id=? ORDER BY created_at DESC",
            (fact_id,),
        ).fetchall()]
        input_ids: list[str] = []
        for row in derivations:
            input_ids.extend(parse_json_array(row["input_fact_ids_json"]))
        input_ids = list(dict.fromkeys(input_ids))
        input_facts: list[dict[str, Any]] = []
        if input_ids:
            marks = ",".join("?" for _ in input_ids)
            input_facts = [dict(row) for row in connection.execute(f"SELECT * FROM semantic_fact WHERE fact_id IN ({marks})", input_ids).fetchall()]
        decisions = [dict(row) for row in connection.execute(
            "SELECT * FROM semantic_rule_decision WHERE decision_id IN (SELECT decision_id FROM semantic_fact_derivation WHERE output_fact_id=?) OR input_fact_ids_json LIKE ? ORDER BY created_at DESC",
            (fact_id, f"%{fact_id}%"),
        ).fetchall()]
        evidence_links = []
        if "semantic_evidence_link" in ontology_meta_tables(connection):
            evidence_links = [dict(row) for row in connection.execute(
                "SELECT * FROM semantic_evidence_link WHERE target_type='fact' AND target_id=? ORDER BY created_at",
                (fact_id,),
            ).fetchall()]
        return {
            "schemaVersion": "explain-v1",
            "fact": dict(fact),
            "inputFacts": input_facts,
            "derivations": derivations,
            "decisions": decisions,
            "evidenceLinks": evidence_links,
            "traceId": ontology_trace_id("FACT", fact_id),
            "sourceWrite": False,
            "formalPublication": False,
        }
    finally:
        connection.close()


def world_model_decision_explain(decision_id: str) -> dict[str, Any]:
    connection = unified_semantics_connection()
    try:
        decision = connection.execute("SELECT * FROM semantic_rule_decision WHERE decision_id=?", (decision_id,)).fetchone()
        if decision is None:
            raise HTTPException(status_code=404, detail="规则判断不存在")
        input_ids = parse_json_array(decision["input_fact_ids_json"])
        input_facts: list[dict[str, Any]] = []
        if input_ids:
            marks = ",".join("?" for _ in input_ids)
            input_facts = [dict(row) for row in connection.execute(f"SELECT * FROM semantic_fact WHERE fact_id IN ({marks})", input_ids).fetchall()]
        derived_facts = [dict(row) for row in connection.execute(
            "SELECT f.* FROM semantic_fact f JOIN semantic_fact_derivation d ON d.output_fact_id=f.fact_id WHERE d.decision_id=? ORDER BY f.created_at",
            (decision_id,),
        ).fetchall()]
        actions = [dict(row) for row in connection.execute(
            "SELECT * FROM semantic_action_plan WHERE decision_id=? ORDER BY created_at",
            (decision_id,),
        ).fetchall()] if "semantic_action_plan" in ontology_meta_tables(connection) else []
        rule = connection.execute(
            "SELECT * FROM semantic_logic_rule WHERE rule_asset_id=? AND rule_version_id=?",
            (decision["rule_asset_id"], decision["rule_version_id"]),
        ).fetchone()
        if rule is None:
            rule = connection.execute(
                "SELECT asset_id AS rule_asset_id,current_version AS rule_version_id,title AS decision,canonical_definition AS explanation,status FROM knowledge_asset WHERE asset_id=? OR asset_key=? LIMIT 1",
                (decision["rule_asset_id"], decision["rule_asset_id"]),
            ).fetchone()
        return {
            "schemaVersion": "explain-v1",
            "decision": dict(decision),
            "rule": dict(rule) if rule else None,
            "inputFacts": input_facts,
            "derivedFacts": derived_facts,
            "actions": actions,
            "traceId": ontology_trace_id("DECISION", decision_id),
            "sourceWrite": False,
            "formalPublication": False,
        }
    finally:
        connection.close()


def world_model_summary() -> dict[str, Any]:
    """Return only materialized world-model capabilities from the local overlay."""
    connection = unified_semantics_connection()
    try:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }

        def count(table: str, where: str = "", parameters: tuple[Any, ...] = ()) -> int:
            if table not in tables:
                return 0
            suffix = f" WHERE {where}" if where else ""
            return int(connection.execute(f"SELECT count(*) FROM {table}{suffix}", parameters).fetchone()[0])

        identity = identity_result_connection()
        try:
            identity_tables = {
                row[0]
                for row in identity.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            }

            def identity_count(table: str) -> int:
                if table not in identity_tables:
                    return 0
                return int(identity.execute(f"SELECT count(*) FROM {table}").fetchone()[0])

            devices = identity_count("unified_device")
            locations = identity_count("function_location")
            identities = identity_count("device_identity_map")
        finally:
            identity.close()
        relations = count("business_object_relation", "status='accepted'")
        events = count(
            "semantic_fact",
            "fact_type IN ('observation_event','defect_event','work_order_event') AND status IN ('observed','accepted')",
        )
        facts = count("semantic_fact", "status<>'retracted'")
        current_state_count = count("semantic_current_state", "status='current'")
        transition_count = count("semantic_state_transition")
        rules = count("semantic_logic_rule", "status='enabled'")
        decisions = count("semantic_rule_decision", "status IN ('accepted','proposed','needs_review')")
        action_plans = count("semantic_action_plan")
        pending_action_approvals = count("semantic_action_plan", "status='PENDING_APPROVAL'")
        evidence_links = count("semantic_fact_derivation", "status IN ('accepted','proposed')")
        pending_status_mappings = count("semantic_status_dictionary", "mapping_status='pending'")
        approved_status_mappings = count("semantic_status_dictionary", "mapping_status='approved'")
        meta_model_run = None
        meta_model_count = 0
        meta_model_gap_count = 0
        if "ontology_meta_model_run" in tables:
            row = connection.execute("SELECT * FROM ontology_meta_model_run ORDER BY created_at DESC LIMIT 1").fetchone()
            meta_model_run = dict(row) if row else None
            if meta_model_run:
                meta_model_count = int(meta_model_run["object_type_count"]) + int(meta_model_run["property_type_count"]) + int(meta_model_run["relation_type_count"]) + int(meta_model_run["event_type_count"]) + int(meta_model_run["transition_rule_count"])
                meta_model_gap_count = int(meta_model_run["unregistered_relation_count"]) + int(meta_model_run["unregistered_event_count"])
        latest_replay = None
        if "semantic_status_replay_run" in tables:
            row = connection.execute(
                "SELECT * FROM semantic_status_replay_run ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
            latest_replay = dict(row) if row else None

        core = [
            {"key": "object", "label": "对象", "count": devices + locations, "status": "active" if devices + locations else "pending", "path": "/unified-devices", "description": f"设备 {devices:,} · 位置 {locations:,}"},
            {"key": "identity", "label": "身份", "count": identities, "status": "active" if identities else "pending", "path": "/unified-devices", "description": "源对象到统一业务对象的身份断言"},
            {"key": "relation", "label": "关系", "count": relations, "status": "active" if relations else "pending", "path": "/unified-devices", "description": "已确认的对象与业务记录关系"},
            {"key": "event", "label": "事件", "count": events, "status": "active" if events else "pending", "path": "/semantic-events", "description": "巡检、缺陷和工单来源事件"},
            {"key": "state", "label": "状态", "count": current_state_count, "status": "active" if current_state_count else ("blocked" if pending_status_mappings else "pending"), "path": "/semantic-status", "description": f"当前状态 {current_state_count} · 状态迁移 {transition_count}"},
            {"key": "fact", "label": "事实", "count": facts, "status": "active" if facts else "pending", "path": "/semantic-facts", "description": f"来源与派生事实；证据链 {evidence_links}"},
            {"key": "meta_model", "label": "元模型", "count": meta_model_count, "status": "active" if meta_model_run and meta_model_gap_count == 0 else ("in_progress" if meta_model_run else "pending"), "path": "/ontology-runtime", "description": f"对象/属性/关系/事件/迁移注册 {meta_model_count}；待复核缺口 {meta_model_gap_count}"},
            {"key": "rule", "label": "规则", "count": rules, "status": "active" if rules else "pending", "path": "/knowledge-assets", "description": "已启用的确定性规则"},
            {"key": "decision", "label": "决策", "count": decisions, "status": "active" if decisions else "pending", "path": "/decisions", "description": "规则判断，与事实分层保存"},
            {"key": "action", "label": "行动", "count": action_plans, "status": "governed" if action_plans else "pending", "path": "/decisions", "description": f"行动计划 {action_plans} · 待审批 {pending_action_approvals}；当前不写源系统"},
        ]
        stages = [
            {"key": "P0", "label": "标准缺陷状态", "status": "completed" if approved_status_mappings and latest_replay and latest_replay.get("status") == "completed" else "in_progress", "detail": f"已确认 {approved_status_mappings} · 待确认 {pending_status_mappings}"},
            {"key": "M1", "label": "本体元模型", "status": "completed" if meta_model_run and meta_model_gap_count == 0 else ("in_progress" if meta_model_run else "pending"), "detail": f"注册 {meta_model_count} 项 · 待复核缺口 {meta_model_gap_count}"},
            {"key": "P1", "label": "统一事件投影", "status": "completed" if events else "pending", "detail": f"已投影 {events} 条来源事件"},
            {"key": "P2", "label": "状态迁移引擎", "status": "completed" if current_state_count and latest_replay and latest_replay.get("status") == "completed" else "pending", "detail": f"当前状态 {current_state_count} · 迁移记录 {transition_count}"},
            {"key": "P3", "label": "分层事实", "status": "in_progress" if facts else "pending", "detail": f"当前事实 {facts} 条"},
            {"key": "P4", "label": "规则体系", "status": "in_progress" if rules else "pending", "detail": f"已启用确定性规则 {rules} 条"},
            {"key": "P5", "label": "决策与行动审批", "status": "completed" if action_plans and pending_action_approvals == 0 else ("in_progress" if decisions else "pending"), "detail": f"决策 {decisions} · 行动计划 {action_plans} · 待审批 {pending_action_approvals}"},
        ]
        return {
            "core": core,
            "stages": stages,
            "sourceWrite": False,
            "formalPublication": False,
            "sourceSystems": ["HD_SAAS", "XNY_SAAS", "MaxiEAM / DM8"],
            "latestStatusReplay": latest_replay,
            "latestOntologyMetaModel": meta_model_run,
            "latestStateTransition": dict(connection.execute("SELECT * FROM semantic_state_transition_run ORDER BY created_at DESC LIMIT 1").fetchone()) if "semantic_state_transition_run" in tables and connection.execute("SELECT 1 FROM semantic_state_transition_run LIMIT 1").fetchone() else None,
            "nextAction": {
                "title": "查看规则决策与行动门禁" if decisions else ("建设事件到状态的后续规则" if current_state_count else "确认缺陷状态映射并运行回放"),
                "description": f"已形成 {decisions} 条规则判断；行动计划 {action_plans} 条，待审批 {pending_action_approvals} 条。" if decisions else (f"已形成 {current_state_count} 条当前状态；下一步补充有时间顺序的事件状态迁移。" if current_state_count else f"{pending_status_mappings} 条原始缺陷状态尚未映射到标准状态。"),
                "path": "/decisions" if decisions else ("/semantic-facts" if current_state_count else "/semantic-status?mapping_status=pending"),
            },
        }
    finally:
        connection.close()


def world_model_coverage() -> dict[str, Any]:
    """Return the latest local evidence-coverage and closure report.

    Missing source evidence is returned as an explicit gap rather than being
    represented as zero business facts without explanation.  This endpoint
    never opens a source-system connection and never changes a review gate.
    """
    connection = unified_semantics_connection()
    try:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if "semantic_coverage_run" not in tables:
            return {
                "schemaVersion": "semantic-coverage-v1",
                "status": "not_initialized",
                "run": None,
                "gaps": [],
                "sourceWrite": False,
                "formalPublication": False,
            }
        run = connection.execute("SELECT * FROM semantic_coverage_run ORDER BY created_at DESC LIMIT 1").fetchone()
        gaps = connection.execute(
            """SELECT gap_id,gap_type,scope_key,expected_count,observed_count,status,
                      evidence_required,note,created_at
               FROM semantic_coverage_gap
               WHERE run_id=?
               ORDER BY CASE status WHEN 'needs_evidence' THEN 0 WHEN 'isolated' THEN 1 ELSE 2 END,
                        gap_type,scope_key""",
            (run["run_id"],),
        ).fetchall() if run else []
        return {
            "schemaVersion": "semantic-coverage-v1",
            "status": run["status"] if run else "not_initialized",
            "run": dict(run) if run else None,
            "gaps": [dict(gap) for gap in gaps],
            "sourceWrite": False,
            "formalPublication": False,
        }
    finally:
        connection.close()


def world_model_runtime_contract() -> dict[str, Any]:
    """Return the source-authority, object and identity runtime contract."""
    connection = unified_semantics_connection()
    try:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if "semantic_runtime_contract_run" not in tables:
            return {"schemaVersion": "semantic-runtime-contract-v1", "status": "not_initialized", "run": None, "authority": [], "counts": {}, "sourceWrite": False, "formalPublication": False}
        run = connection.execute("SELECT * FROM semantic_runtime_contract_run ORDER BY created_at DESC LIMIT 1").fetchone()
        authority = connection.execute(
            "SELECT layer_key,layer_kind,display_name,precedence,source_of_record,requires_approval,applies_to_json,status,version FROM semantic_layer_authority WHERE status='active' ORDER BY precedence DESC,layer_key"
        ).fetchall()
        violation_rows = connection.execute(
            "SELECT constraint_key,status,severity,count(*) AS count FROM semantic_constraint_violation GROUP BY constraint_key,status,severity ORDER BY severity DESC,constraint_key"
        ).fetchall()
        counts = {
            "objectInstances": int(connection.execute("SELECT count(*) FROM semantic_object_instance").fetchone()[0]),
            "identityAssertions": int(connection.execute("SELECT count(*) FROM semantic_identity_assertion").fetchone()[0]),
            "events": int(connection.execute("SELECT count(*) FROM semantic_event WHERE status IN ('observed','accepted')").fetchone()[0]),
            "tracedEvents": int(connection.execute("SELECT count(*) FROM semantic_event WHERE provenance_hash IS NOT NULL AND previous_event_id IS NOT NULL OR provenance_hash IS NOT NULL").fetchone()[0]),
            "violations": int(connection.execute("SELECT count(*) FROM semantic_constraint_violation").fetchone()[0]),
        }
        return {
            "schemaVersion": "semantic-runtime-contract-v1",
            "status": run["status"] if run else "not_initialized",
            "run": dict(run) if run else None,
            "authority": [dict(row) for row in authority],
            "counts": counts,
            "violations": [dict(row) for row in violation_rows],
            "sourceWrite": False,
            "formalPublication": False,
        }
    finally:
        connection.close()


def world_model_governance_contract() -> dict[str, Any]:
    """Return typed identity, relation, schema, event and rule governance.

    This is a read-only view over the local semantic overlay.  It exposes
    isolated evidence gaps explicitly and never turns a missing relation into
    an inferred business fact.
    """
    connection = unified_semantics_connection()
    try:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        required = {"semantic_governance_quality_run", "semantic_identity_policy", "semantic_relation_contract", "semantic_object_property", "semantic_executable_rule"}
        if not required.issubset(tables):
            return {
                "schemaVersion": "semantic-governance-contract-v1",
                "status": "not_initialized",
                "run": None,
                "identity": {},
                "relations": [],
                "objectSchema": [],
                "eventRelations": [],
                "rules": [],
                "conflicts": [],
                "violations": [],
                "sourceWrite": False,
                "formalPublication": False,
            }
        run = connection.execute("SELECT * FROM semantic_governance_quality_run ORDER BY created_at DESC LIMIT 1").fetchone()
        policy = connection.execute("SELECT * FROM semantic_identity_policy WHERE status='active' ORDER BY effective_from DESC LIMIT 1").fetchone()
        identity_summary = {
            "policy": dict(policy) if policy else None,
            "assertionCount": int(connection.execute("SELECT count(*) FROM semantic_identity_assertion").fetchone()[0]),
            "autoCount": int(connection.execute("SELECT count(*) FROM semantic_identity_assertion WHERE decision_mode='auto'").fetchone()[0]),
            "manualCount": int(connection.execute("SELECT count(*) FROM semantic_identity_assertion WHERE decision_mode='manual'").fetchone()[0]),
            "reviewRequiredCount": int(connection.execute("SELECT count(*) FROM semantic_identity_assertion WHERE review_required=1").fetchone()[0]),
            "revokedCount": int(connection.execute("SELECT count(*) FROM semantic_identity_assertion WHERE lifecycle_status='revoked'").fetchone()[0]),
            "conflictCount": int(connection.execute("SELECT count(*) FROM semantic_identity_conflict WHERE status='isolated'").fetchone()[0]),
        }
        if "semantic_identity_review" in tables:
            identity_summary["reviewQueue"] = {
                str(row["queue_status"]): int(row["count"])
                for row in connection.execute("SELECT queue_status,count(*) AS count FROM semantic_identity_review GROUP BY queue_status").fetchall()
            }
        relation_contracts = [dict(row) for row in connection.execute(
            "SELECT relation_type,predicate,subject_type,object_type,min_cardinality,max_cardinality,temporal,evidence_required,version,review_status,status FROM semantic_relation_contract WHERE status='active' ORDER BY relation_type"
        ).fetchall()]
        object_schema = [dict(row) for row in connection.execute(
            "SELECT object_type,property_key,value_type,required,repeatable,unit,source_mapping_json,validation_json,version,review_status,status FROM semantic_object_property WHERE status='active' ORDER BY object_type,property_key"
        ).fetchall()]
        event_relations = [dict(row) for row in connection.execute(
            "SELECT relation_type,predicate,subject_event_type,object_event_type,min_cardinality,max_cardinality,evidence_required,version,review_status,status FROM semantic_event_relation_contract WHERE status='active' ORDER BY relation_type"
        ).fetchall()] if "semantic_event_relation_contract" in tables else []
        rules = [dict(row) for row in connection.execute(
            "SELECT rule_id,rule_version,rule_type,title,target_object_type,condition_json,result_schema_json,action_json,effective_from,effective_to,status,requires_approval,source_write,formal_publication FROM semantic_executable_rule WHERE status IN ('replayed','approved','enabled') ORDER BY rule_type,rule_id,rule_version"
        ).fetchall()]
        conflicts = [dict(row) for row in connection.execute(
            "SELECT conflict_id,conflict_type,source_schema,source_table,source_row_id,source_key_type,source_key,canonical_object_ids_json,assertion_ids_json,severity,status,evidence_json,detected_at FROM semantic_identity_conflict WHERE status='isolated' ORDER BY detected_at DESC,conflict_id"
        ).fetchall()]
        violations = [dict(row) for row in connection.execute(
            "SELECT constraint_key,entity_type,entity_id,severity,status,expected_json,actual_json,evidence_json,created_at FROM semantic_constraint_violation WHERE constraint_key LIKE 'governance_%' ORDER BY severity DESC,constraint_key,entity_id"
        ).fetchall()]
        return {
            "schemaVersion": "semantic-governance-contract-v1",
            "status": run["status"] if run else "not_initialized",
            "run": dict(run) if run else None,
            "identity": identity_summary,
            "relations": relation_contracts,
            "objectSchema": object_schema,
            "eventRelations": event_relations,
            "rules": rules,
            "conflicts": conflicts,
            "violations": violations,
            "sourceWrite": False,
            "formalPublication": False,
        }
    finally:
        connection.close()


def revoke_semantic_identity(assertion_id: str, request: SemanticIdentityRevokeRequest, actor: str = Depends(require_decision_auth)) -> dict[str, Any]:
    """Revoke one local identity assertion with an auditable reason.

    Revocation changes only the local overlay lifecycle.  It deliberately does
    not change the source record, source snapshot, or canonical source key.
    """
    connection = unified_semantics_write_connection()
    try:
        assertion = connection.execute(
            "SELECT assertion_id,canonical_object_id,lifecycle_status,status,decision_mode FROM semantic_identity_assertion WHERE assertion_id=?",
            (assertion_id,),
        ).fetchone()
        if assertion is None:
            raise HTTPException(status_code=404, detail="身份断言不存在")
        existing = connection.execute(
            "SELECT audit_id,created_at,revoked_by,revocation_reason FROM semantic_identity_revocation_audit WHERE idempotency_key=?",
            (request.idempotency_key,),
        ).fetchone()
        if existing is not None:
            return {
                "status": "already_revoked",
                "assertion": dict(assertion),
                "audit": dict(existing),
                "sourceWrite": False,
                "formalPublication": False,
            }
        if assertion["lifecycle_status"] == "revoked":
            return {
                "status": "already_revoked",
                "assertion": dict(assertion),
                "audit": None,
                "sourceWrite": False,
                "formalPublication": False,
            }
        timestamp = utc_now()
        audit_id = f"SIRA-{hashlib.sha256((assertion_id + request.idempotency_key).encode('utf-8')).hexdigest()[:24]}"
        connection.execute(
            """
            INSERT INTO semantic_identity_revocation_audit(
              audit_id,assertion_id,previous_lifecycle_status,revoked_by,revocation_reason,idempotency_key,created_at
            ) VALUES (?,?,?,?,?,?,?)
            """,
            (audit_id, assertion_id, assertion["lifecycle_status"], request.reviewer.strip() or actor, request.reason.strip(), request.idempotency_key, timestamp),
        )
        connection.execute(
            """
            UPDATE semantic_identity_assertion
               SET lifecycle_status='revoked',revoked_at=?,revoked_by=?,revocation_reason=?,review_required=1,updated_at=?
             WHERE assertion_id=?
            """,
            (timestamp, request.reviewer.strip() or actor, request.reason.strip(), timestamp, assertion_id),
        )
        connection.commit()
        updated = connection.execute("SELECT * FROM semantic_identity_assertion WHERE assertion_id=?", (assertion_id,)).fetchone()
        return {
            "status": "revoked",
            "assertion": dict(updated) if updated else None,
            "audit": {"auditId": audit_id, "reviewer": request.reviewer.strip() or actor, "reason": request.reason.strip(), "createdAt": timestamp},
            "sourceWrite": False,
            "formalPublication": False,
        }
    except HTTPException:
        connection.rollback()
        raise
    except sqlite3.IntegrityError as exc:
        connection.rollback()
        raise HTTPException(status_code=409, detail=f"身份撤销审计冲突：{exc}") from exc
    finally:
        connection.close()


def semantic_identity_review_queue(
    status: Literal["all", "pending", "approved", "rejected", "blocked"] = "pending",
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    """List local identity assertions that require an explicit decision."""
    connection = unified_semantics_connection()
    try:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if "semantic_identity_review" not in tables:
            return {"schemaVersion": "semantic-identity-review-v1", "status": "not_initialized", "rows": [], "total": 0, "page": page, "pageSize": page_size, "sourceWrite": False, "formalPublication": False}
        where = ""
        parameters: list[Any] = []
        if status != "all":
            where = "WHERE r.queue_status=?"
            parameters.append(status)
        total = int(connection.execute(f"SELECT count(*) FROM semantic_identity_review r {where}", parameters).fetchone()[0])
        parameters.extend([page_size, (page - 1) * page_size])
        rows = [dict(row) for row in connection.execute(
            f"""
            SELECT r.review_id,r.assertion_id,r.queue_status,r.reviewer,r.review_note,r.evidence_json,
                   r.approval_receipt,r.created_at,r.reviewed_at,r.updated_at,
                   a.source_system,a.source_schema,a.source_table_group,a.source_table,a.source_row_id,
                   a.source_key_type,a.source_key,a.canonical_object_type,a.canonical_object_id,
                   a.assertion_type,a.status AS assertion_status,a.confidence,a.valid_from,a.valid_to,
                   a.decision_mode,a.review_required,a.lifecycle_status,a.policy_version,a.evidence_json AS assertion_evidence_json,
                   a.source_snapshot_id
              FROM semantic_identity_review r
              JOIN semantic_identity_assertion a ON a.assertion_id=r.assertion_id
              {where}
             ORDER BY CASE r.queue_status WHEN 'pending' THEN 0 WHEN 'blocked' THEN 1 ELSE 2 END,
                      r.created_at,r.review_id
             LIMIT ? OFFSET ?
            """,
            parameters,
        ).fetchall()]
        conflict_by_assertion: dict[str, list[dict[str, Any]]] = {}
        if "semantic_identity_conflict" in tables:
            for conflict in connection.execute("SELECT * FROM semantic_identity_conflict WHERE status='isolated'").fetchall():
                item = dict(conflict)
                for assertion_id in parse_json_array(conflict["assertion_ids_json"]):
                    conflict_by_assertion.setdefault(assertion_id, []).append(item)
        for row in rows:
            row["conflicts"] = conflict_by_assertion.get(str(row["assertion_id"]), [])
            row["hasConflict"] = bool(row["conflicts"])
        counts = {
            str(row["queue_status"]): int(row["count"])
            for row in connection.execute("SELECT queue_status,count(*) AS count FROM semantic_identity_review GROUP BY queue_status ORDER BY queue_status").fetchall()
        }
        return {
            "schemaVersion": "semantic-identity-review-v1",
            "status": "ready",
            "rows": rows,
            "total": total,
            "page": page,
            "pageSize": page_size,
            "counts": counts,
            "sourceWrite": False,
            "formalPublication": False,
        }
    finally:
        connection.close()


def semantic_identity_review_detail(review_id: str) -> dict[str, Any]:
    connection = unified_semantics_connection()
    try:
        row = connection.execute(
            """
            SELECT r.*,a.source_system,a.source_schema,a.source_table_group,a.source_table,a.source_row_id,
                   a.source_key_type,a.source_key,a.canonical_object_type,a.canonical_object_id,a.assertion_type,
                   a.status AS assertion_status,a.confidence,a.valid_from,a.valid_to,a.decision_mode,a.review_required,
                   a.lifecycle_status,a.policy_version,a.evidence_json AS assertion_evidence_json,a.source_snapshot_id
              FROM semantic_identity_review r
              JOIN semantic_identity_assertion a ON a.assertion_id=r.assertion_id
             WHERE r.review_id=?
            """,
            (review_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="身份审核记录不存在")
        conflicts = [dict(item) for item in connection.execute(
            "SELECT * FROM semantic_identity_conflict WHERE status='isolated' AND assertion_ids_json LIKE ? ORDER BY detected_at DESC",
            (f"%{row['assertion_id']}%",),
        ).fetchall()]
        audits = [dict(item) for item in connection.execute(
            "SELECT * FROM semantic_identity_review_audit WHERE review_id=? ORDER BY created_at DESC",
            (review_id,),
        ).fetchall()] if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='semantic_identity_review_audit'").fetchone() else []
        return {
            "schemaVersion": "semantic-identity-review-v1",
            "review": dict(row),
            "conflicts": conflicts,
            "audits": audits,
            "sourceWrite": False,
            "formalPublication": False,
        }
    finally:
        connection.close()


def decide_semantic_identity_review(review_id: str, request: SemanticIdentityReviewRequest, actor: str = Depends(require_decision_auth)) -> dict[str, Any]:
    """Record a local identity review decision; source systems remain read-only."""
    connection = unified_semantics_write_connection()
    try:
        review = connection.execute("SELECT * FROM semantic_identity_review WHERE review_id=?", (review_id,)).fetchone()
        if review is None:
            raise HTTPException(status_code=404, detail="身份审核记录不存在")
        assertion = connection.execute("SELECT * FROM semantic_identity_assertion WHERE assertion_id=?", (review["assertion_id"],)).fetchone()
        if assertion is None:
            raise HTTPException(status_code=404, detail="身份断言不存在")
        prior = connection.execute("SELECT * FROM semantic_identity_review_audit WHERE idempotency_key=?", (request.idempotency_key,)).fetchone()
        if prior is not None:
            return {"status": "already_decided", "review": dict(review), "audit": dict(prior), "sourceWrite": False, "formalPublication": False}
        if review["queue_status"] != "pending":
            return {"status": "already_decided", "review": dict(review), "audit": None, "sourceWrite": False, "formalPublication": False}
        conflicts = connection.execute(
            "SELECT conflict_id,conflict_type,source_key_type,source_key FROM semantic_identity_conflict WHERE status='isolated' AND assertion_ids_json LIKE ?",
            (f"%{review['assertion_id']}%",),
        ).fetchall()
        if request.decision == "approved" and conflicts:
            raise HTTPException(status_code=409, detail="该身份断言属于冲突组，必须先处理同组其他映射；不能单条自动解除冲突")
        timestamp = utc_now()
        receipt = f"IRV-{hashlib.sha256((review_id + request.decision + request.idempotency_key).encode('utf-8')).hexdigest()[:24]}"
        audit_id = f"SIRA-{hashlib.sha256((review_id + request.idempotency_key).encode('utf-8')).hexdigest()[:24]}"
        connection.execute(
            """
            INSERT INTO semantic_identity_review_audit(
              audit_id,review_id,assertion_id,decision,reviewer,note,evidence_json,approval_receipt,idempotency_key,created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            (audit_id, review_id, review["assertion_id"], request.decision, request.reviewer.strip() or actor, request.note.strip(), json.dumps(request.evidence, ensure_ascii=False, sort_keys=True), receipt, request.idempotency_key, timestamp),
        )
        new_assertion_status = "accepted" if request.decision == "approved" else "rejected"
        new_link_status = "accepted" if request.decision == "approved" else "blocked"
        new_relation_status = "accepted" if request.decision == "approved" else "isolated"
        connection.execute(
            """
            UPDATE semantic_identity_review
               SET queue_status=?,reviewer=?,review_note=?,evidence_json=?,approval_receipt=?,idempotency_key=?,reviewed_at=?,updated_at=?
             WHERE review_id=?
            """,
            (request.decision, request.reviewer.strip() or actor, request.note.strip(), json.dumps(request.evidence, ensure_ascii=False, sort_keys=True), receipt, request.idempotency_key, timestamp, timestamp, review_id),
        )
        connection.execute(
            """
            UPDATE semantic_identity_assertion
               SET status=?,decision_mode='manual',review_required=0,updated_at=?
             WHERE assertion_id=?
            """,
            (new_assertion_status, timestamp, review["assertion_id"]),
        )
        connection.execute(
            """
            UPDATE business_record_link
               SET status=?
             WHERE source_schema=? AND source_table=? AND source_row_id=?
            """,
            (new_link_status, assertion["source_schema"], assertion["source_table"], assertion["source_row_id"]),
        )
        if "semantic_relation_assertion" in {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}:
            connection.execute(
                """
                UPDATE semantic_relation_assertion
                   SET status=?,updated_at=?
                 WHERE source_schema=? AND source_table=? AND source_row_id=?
                """,
                (new_relation_status, timestamp, assertion["source_schema"], assertion["source_table"], assertion["source_row_id"]),
            )
        connection.commit()
        updated = connection.execute("SELECT * FROM semantic_identity_review WHERE review_id=?", (review_id,)).fetchone()
        return {
            "status": request.decision,
            "review": dict(updated) if updated else None,
            "approvalReceipt": receipt,
            "replayRequired": True,
            "sourceWrite": False,
            "formalPublication": False,
        }
    except HTTPException:
        connection.rollback()
        raise
    except sqlite3.IntegrityError as exc:
        connection.rollback()
        raise HTTPException(status_code=409, detail=f"身份审核写入冲突：{exc}") from exc
    finally:
        connection.close()



def build_router() -> APIRouter:
    router = APIRouter()
    router.add_api_route("/api/world-model/device/{unified_device_id}/context", world_model_device_context, methods=["GET"])
    router.add_api_route("/api/world-model/device/{unified_device_id}/timeline", world_model_device_timeline, methods=["GET"])
    router.add_api_route("/api/world-model/facts/{fact_id}/explain", world_model_fact_explain, methods=["GET"])
    router.add_api_route("/api/world-model/decisions/{decision_id}/explain", world_model_decision_explain, methods=["GET"])
    router.add_api_route("/api/world-model/summary", world_model_summary, methods=["GET"])
    router.add_api_route("/api/world-model/coverage", world_model_coverage, methods=["GET"])
    router.add_api_route("/api/world-model/runtime-contract", world_model_runtime_contract, methods=["GET"])
    router.add_api_route("/api/world-model/governance-contract", world_model_governance_contract, methods=["GET"])
    router.add_api_route("/api/world-model/identity/{assertion_id}/revoke", revoke_semantic_identity, methods=["POST"])
    router.add_api_route("/api/world-model/identity-review", semantic_identity_review_queue, methods=["GET"])
    router.add_api_route("/api/world-model/identity-review/{review_id}", semantic_identity_review_detail, methods=["GET"])
    router.add_api_route("/api/world-model/identity-review/{review_id}", decide_semantic_identity_review, methods=["POST"])
    return router
