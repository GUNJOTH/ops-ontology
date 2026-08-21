"""World-model summary, coverage and governance contract services."""
from __future__ import annotations

from typing import Any

from app.core.db import identity_result_connection, unified_semantics_connection


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
