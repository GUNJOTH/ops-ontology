"""Build the executable governance contract for the local semantic overlay.

This module turns the five high-priority ontology concerns into explicit,
versioned relational contracts:

* identity mapping policy, conflict isolation and revocation audit;
* typed business-record relation contracts;
* object property and lifecycle schemas;
* event-causality contracts (without guessing absent links);
* executable, versioned rule definitions and quality metrics.

The target is the local semantic overlay only.  Source snapshots and DM8 /
MaxiEAM / HD_SAAS / XNY_SAAS tables are never written by this module.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import pathlib
import sqlite3
from datetime import datetime, timezone
from typing import Any

from pipeline.contracts import connect_local

from semantic_predicates import event_predicate, relation_predicate


ROOT = pathlib.Path(__file__).resolve().parent
DEFAULT_TARGET = ROOT / "data" / "unified_semantics.sqlite3"
CONTRACT_VERSION = "semantic-governance-contract-v1"


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sid(prefix: str, *parts: object) -> str:
    raw = "|".join("" if part is None else str(part) for part in parts)
    return f"{prefix}-{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:24]}"


def json_text(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def parse_json(value: object) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "{}") if isinstance(value, str) else value
    except (TypeError, json.JSONDecodeError):
        parsed = {}
    return parsed if isinstance(parsed, dict) else {}


def safe_confidence(value: object) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(parsed):
        return 0.0
    return max(0.0, min(1.0, parsed))


def ensure_schema(db: sqlite3.Connection) -> None:
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS semantic_identity_policy (
          policy_id TEXT PRIMARY KEY,
          policy_version TEXT NOT NULL,
          auto_conditions_json TEXT NOT NULL,
          manual_conditions_json TEXT NOT NULL,
          conflict_policy_json TEXT NOT NULL,
          cardinality_policy_json TEXT NOT NULL,
          revocation_policy_json TEXT NOT NULL,
          status TEXT NOT NULL CHECK(status IN ('draft','active','retired','blocked')),
          effective_from TEXT NOT NULL,
          effective_to TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          UNIQUE(policy_id, policy_version)
        );
        CREATE INDEX IF NOT EXISTS ix_semantic_identity_policy_status
          ON semantic_identity_policy(status,effective_from);

        CREATE TABLE IF NOT EXISTS semantic_identity_conflict (
          conflict_id TEXT PRIMARY KEY,
          conflict_type TEXT NOT NULL,
          source_schema TEXT NOT NULL,
          source_table TEXT,
          source_row_id TEXT,
          source_key_type TEXT,
          source_key TEXT,
          canonical_object_ids_json TEXT NOT NULL,
          assertion_ids_json TEXT NOT NULL,
          severity TEXT NOT NULL CHECK(severity IN ('low','medium','high','critical')),
          status TEXT NOT NULL CHECK(status IN ('open','isolated','resolved')),
          evidence_json TEXT NOT NULL,
          detected_at TEXT NOT NULL,
          resolved_at TEXT,
          UNIQUE(conflict_type,source_schema,source_table,source_row_id,source_key_type,source_key)
        );
        CREATE INDEX IF NOT EXISTS ix_semantic_identity_conflict_status
          ON semantic_identity_conflict(status,severity,detected_at);

        CREATE TABLE IF NOT EXISTS semantic_identity_revocation_audit (
          audit_id TEXT PRIMARY KEY,
          assertion_id TEXT NOT NULL,
          previous_lifecycle_status TEXT NOT NULL,
          revoked_by TEXT NOT NULL,
          revocation_reason TEXT NOT NULL,
          idempotency_key TEXT NOT NULL UNIQUE,
          created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_semantic_identity_revocation_assertion
          ON semantic_identity_revocation_audit(assertion_id,created_at);

        CREATE TABLE IF NOT EXISTS semantic_identity_review (
          review_id TEXT PRIMARY KEY,
          assertion_id TEXT NOT NULL UNIQUE,
          queue_status TEXT NOT NULL CHECK(queue_status IN ('pending','approved','rejected','blocked')),
          reviewer TEXT,
          review_note TEXT NOT NULL DEFAULT '',
          evidence_json TEXT NOT NULL DEFAULT '{}',
          approval_receipt TEXT,
          idempotency_key TEXT,
          created_at TEXT NOT NULL,
          reviewed_at TEXT,
          updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_semantic_identity_review_queue
          ON semantic_identity_review(queue_status,created_at);

        CREATE TABLE IF NOT EXISTS semantic_identity_review_audit (
          audit_id TEXT PRIMARY KEY,
          review_id TEXT NOT NULL,
          assertion_id TEXT NOT NULL,
          decision TEXT NOT NULL CHECK(decision IN ('approved','rejected')),
          reviewer TEXT NOT NULL,
          note TEXT NOT NULL,
          evidence_json TEXT NOT NULL,
          approval_receipt TEXT NOT NULL,
          idempotency_key TEXT NOT NULL UNIQUE,
          created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_semantic_identity_review_audit_assertion
          ON semantic_identity_review_audit(assertion_id,created_at);

        CREATE TABLE IF NOT EXISTS semantic_relation_contract (
          contract_id TEXT PRIMARY KEY,
          relation_type TEXT NOT NULL,
          predicate TEXT NOT NULL,
          subject_type TEXT NOT NULL,
          object_type TEXT NOT NULL,
          min_cardinality INTEGER NOT NULL,
          max_cardinality INTEGER,
          temporal INTEGER NOT NULL CHECK(temporal IN (0,1)),
          evidence_required INTEGER NOT NULL CHECK(evidence_required IN (0,1)),
          source_business_types_json TEXT NOT NULL,
          version TEXT NOT NULL,
          review_status TEXT NOT NULL CHECK(review_status IN ('draft','approved','needs_review','retired')),
          status TEXT NOT NULL CHECK(status IN ('active','blocked','retired')),
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          UNIQUE(relation_type,version)
        );
        CREATE INDEX IF NOT EXISTS ix_semantic_relation_contract_active
          ON semantic_relation_contract(status,review_status,relation_type);

        CREATE TABLE IF NOT EXISTS semantic_relation_assertion (
          relation_id TEXT PRIMARY KEY,
          relation_type TEXT NOT NULL,
          subject_type TEXT NOT NULL,
          subject_key TEXT NOT NULL,
          object_type TEXT NOT NULL,
          object_key TEXT NOT NULL,
          source_schema TEXT NOT NULL,
          source_table TEXT NOT NULL,
          source_row_id TEXT NOT NULL,
          source_snapshot_id TEXT,
          status TEXT NOT NULL CHECK(status IN ('accepted','needs_review','rejected','isolated')),
          confidence REAL NOT NULL CHECK(confidence >= 0 AND confidence <= 1),
          evidence_json TEXT NOT NULL,
          valid_from TEXT,
          valid_to TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          UNIQUE(relation_type,subject_type,subject_key,object_type,object_key)
        );
        CREATE INDEX IF NOT EXISTS ix_semantic_relation_assertion_subject
          ON semantic_relation_assertion(subject_type,subject_key,status);
        CREATE INDEX IF NOT EXISTS ix_semantic_relation_assertion_object
          ON semantic_relation_assertion(object_type,object_key,status);

        CREATE TABLE IF NOT EXISTS semantic_object_property (
          property_id TEXT PRIMARY KEY,
          object_type TEXT NOT NULL,
          property_key TEXT NOT NULL,
          value_type TEXT NOT NULL,
          required INTEGER NOT NULL CHECK(required IN (0,1)),
          repeatable INTEGER NOT NULL CHECK(repeatable IN (0,1)),
          unit TEXT,
          source_mapping_json TEXT NOT NULL,
          validation_json TEXT NOT NULL,
          version TEXT NOT NULL,
          review_status TEXT NOT NULL CHECK(review_status IN ('draft','approved','needs_review','retired')),
          status TEXT NOT NULL CHECK(status IN ('active','blocked','retired')),
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          UNIQUE(object_type,property_key,version)
        );
        CREATE INDEX IF NOT EXISTS ix_semantic_object_property_object
          ON semantic_object_property(object_type,status,required);

        CREATE TABLE IF NOT EXISTS semantic_object_lifecycle (
          lifecycle_id TEXT PRIMARY KEY,
          object_type TEXT NOT NULL,
          state TEXT NOT NULL,
          initial_state INTEGER NOT NULL CHECK(initial_state IN (0,1)),
          terminal_state INTEGER NOT NULL CHECK(terminal_state IN (0,1)),
          allowed_next_json TEXT NOT NULL,
          version TEXT NOT NULL,
          review_status TEXT NOT NULL CHECK(review_status IN ('draft','approved','needs_review','retired')),
          status TEXT NOT NULL CHECK(status IN ('active','blocked','retired')),
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          UNIQUE(object_type,state,version)
        );

        CREATE TABLE IF NOT EXISTS semantic_event_relation_contract (
          contract_id TEXT PRIMARY KEY,
          relation_type TEXT NOT NULL,
          predicate TEXT NOT NULL,
          subject_event_type TEXT NOT NULL,
          object_event_type TEXT NOT NULL,
          min_cardinality INTEGER NOT NULL,
          max_cardinality INTEGER,
          evidence_required INTEGER NOT NULL CHECK(evidence_required IN (0,1)),
          version TEXT NOT NULL,
          review_status TEXT NOT NULL CHECK(review_status IN ('draft','approved','needs_review','retired')),
          status TEXT NOT NULL CHECK(status IN ('active','blocked','retired')),
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          UNIQUE(relation_type,version)
        );

        CREATE TABLE IF NOT EXISTS semantic_event_relation (
          relation_id TEXT PRIMARY KEY,
          relation_type TEXT NOT NULL,
          subject_event_id TEXT NOT NULL,
          predicate TEXT NOT NULL,
          object_event_id TEXT NOT NULL,
          status TEXT NOT NULL CHECK(status IN ('accepted','needs_review','rejected','isolated')),
          confidence REAL NOT NULL CHECK(confidence >= 0 AND confidence <= 1),
          evidence_json TEXT NOT NULL,
          source_schema TEXT,
          source_table TEXT,
          source_row_id TEXT,
          source_snapshot_id TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          UNIQUE(relation_type,subject_event_id,object_event_id)
        );

        CREATE TABLE IF NOT EXISTS semantic_executable_rule (
          rule_id TEXT NOT NULL,
          rule_version TEXT NOT NULL,
          rule_type TEXT NOT NULL CHECK(rule_type IN ('identity','relation','event','fact','decision','action')),
          title TEXT NOT NULL,
          target_object_type TEXT NOT NULL,
          input_schema_json TEXT NOT NULL,
          condition_json TEXT NOT NULL,
          result_schema_json TEXT NOT NULL,
          action_json TEXT NOT NULL,
          effective_from TEXT NOT NULL,
          effective_to TEXT,
          source_asset_id TEXT,
          source_version_id TEXT,
          status TEXT NOT NULL CHECK(status IN ('draft','replayed','approved','enabled','retired','blocked')),
          requires_approval INTEGER NOT NULL CHECK(requires_approval IN (0,1)),
          source_write INTEGER NOT NULL CHECK(source_write=0),
          formal_publication INTEGER NOT NULL CHECK(formal_publication=0),
          provenance_json TEXT NOT NULL,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          PRIMARY KEY(rule_id,rule_version)
        );
        CREATE INDEX IF NOT EXISTS ix_semantic_executable_rule_active
          ON semantic_executable_rule(status,rule_type,target_object_type);

        CREATE TABLE IF NOT EXISTS semantic_governance_quality_run (
          run_id TEXT PRIMARY KEY,
          contract_version TEXT NOT NULL,
          object_schema_count INTEGER NOT NULL,
          relation_contract_count INTEGER NOT NULL,
          relation_assertion_count INTEGER NOT NULL,
          identity_conflict_count INTEGER NOT NULL,
          event_relation_count INTEGER NOT NULL,
          executable_rule_count INTEGER NOT NULL,
          orphan_relation_count INTEGER NOT NULL,
          location_evidence_gap_count INTEGER NOT NULL,
          causal_evidence_gap_count INTEGER NOT NULL,
          missing_provenance_count INTEGER NOT NULL,
          critical_count INTEGER NOT NULL,
          isolated_count INTEGER NOT NULL,
          status TEXT NOT NULL CHECK(status IN ('completed','completed_with_isolation','blocked')),
          source_write INTEGER NOT NULL CHECK(source_write=0),
          formal_publication INTEGER NOT NULL CHECK(formal_publication=0),
          created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_semantic_governance_quality_run_created
          ON semantic_governance_quality_run(created_at);
        """
    )
    columns = {row[1] for row in db.execute("PRAGMA table_info(semantic_identity_assertion)").fetchall()}
    additive = {
        "decision_mode": "TEXT NOT NULL DEFAULT 'derived'",
        "review_required": "INTEGER NOT NULL DEFAULT 0",
        "lifecycle_status": "TEXT NOT NULL DEFAULT 'active'",
        "policy_version": "TEXT",
        "revoked_at": "TEXT",
        "revoked_by": "TEXT",
        "revocation_reason": "TEXT",
    }
    for column, definition in additive.items():
        if column not in columns:
            db.execute(f"ALTER TABLE semantic_identity_assertion ADD COLUMN {column} {definition}")


def seed_identity_policy(db: sqlite3.Connection, created: str) -> None:
    db.execute(
        """
        INSERT INTO semantic_identity_policy(
          policy_id,policy_version,auto_conditions_json,manual_conditions_json,
          conflict_policy_json,cardinality_policy_json,revocation_policy_json,
          status,effective_from,created_at,updated_at
        ) VALUES (?,?,?,?,? ,? ,? ,'active',?,?,?)
        ON CONFLICT(policy_id,policy_version) DO UPDATE SET
          auto_conditions_json=excluded.auto_conditions_json,
          manual_conditions_json=excluded.manual_conditions_json,
          conflict_policy_json=excluded.conflict_policy_json,
          cardinality_policy_json=excluded.cardinality_policy_json,
          revocation_policy_json=excluded.revocation_policy_json,
          status='active',updated_at=excluded.updated_at
        """,
        (
            "SIP:device-identity",
            "SIPV:device-identity-v1",
            json_text({
                "all": [
                    "source_record_is_unique_to_one_canonical_object",
                    "accepted_evidence_is_present",
                    {"confidence_gte": 0.99},
                    "no_active_conflict",
                ],
                "allowed_key_types": ["ASSETNUM", "ASSETNUM1"],
            }),
            json_text({
                "any": [
                    "confidence_lt_0.99",
                    "context_only_match",
                    "KKS_or_location_fuzzy_match",
                    "source_key_not_unique",
                ],
                "result": "keep_mapping_review_required",
            }),
            json_text({
                "source_record_to_multiple_canonical": "isolate_and_review",
                "overlapping_temporal_assertions": "isolate_and_review",
                "never_auto_merge": True,
            }),
            json_text({
                "source_record": "0_or_1_active_canonical_object",
                "canonical_object": "many_source_identities_allowed",
                "temporal_reuse": "allowed_only_when_validity_intervals_do_not_overlap",
            }),
            json_text({
                "allowed": True,
                "mechanism": "set lifecycle_status=revoked and retain reviewer/reason",
                "source_write": False,
                "rebuild_required": True,
            }),
            created,
            created,
            created,
        ),
    )


def seed_relation_contracts(db: sqlite3.Connection, created: str) -> None:
    rows = [
        ("device_has_inspection", "device", "inspection", 0, None, 1, 1, ["inspection"]),
        ("device_has_defect", "device", "defect", 0, None, 1, 1, ["defect"]),
        ("device_has_work_order", "device", "work_order", 0, None, 1, 1, ["work_order"]),
        ("defect_has_work_order", "defect", "work_order", 0, None, 1, 1, ["defect", "work_order"]),
        ("inspection_has_defect", "inspection", "defect", 0, None, 1, 1, ["inspection", "defect"]),
        ("device_located_at", "device", "location", 0, 1, 1, 1, ["device", "location"]),
    ]
    for relation_type, subject, object_type, minimum, maximum, temporal, evidence, source_types in rows:
        predicate = relation_predicate(relation_type)
        db.execute(
            """
            INSERT INTO semantic_relation_contract(
              contract_id,relation_type,predicate,subject_type,object_type,min_cardinality,
              max_cardinality,temporal,evidence_required,source_business_types_json,version,
              review_status,status,created_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,'approved','active',?,?)
            ON CONFLICT(relation_type,version) DO UPDATE SET
              predicate=excluded.predicate,subject_type=excluded.subject_type,object_type=excluded.object_type,
              min_cardinality=excluded.min_cardinality,max_cardinality=excluded.max_cardinality,
              temporal=excluded.temporal,evidence_required=excluded.evidence_required,
              source_business_types_json=excluded.source_business_types_json,
              review_status='approved',status='active',updated_at=excluded.updated_at
            """,
            (
                sid("SRC", relation_type, CONTRACT_VERSION), relation_type, predicate, subject,
                object_type, minimum, maximum, temporal, evidence, json_text(source_types),
                CONTRACT_VERSION, created, created,
            ),
        )


def seed_object_schema(db: sqlite3.Connection, created: str) -> None:
    rows = [
        ("device", "canonical_key", "string", 1, 0, None, {"column": "semantic_object_instance.canonical_key"}, {"non_empty": True}),
        ("device", "source_namespace", "string", 1, 0, None, {"column": "semantic_object_instance.source_namespace"}, {"non_empty": True}),
        ("device", "source_snapshot_id", "string", 1, 0, None, {"column": "semantic_object_instance.source_snapshot_id"}, {"non_empty": True}),
        ("device", "location", "reference", 0, 1, None, {"relation": "device_located_at"}, {"temporal": True}),
        ("device", "kks", "string", 0, 1, None, {"source_fields": ["KKS", "KKS_CODE"]}, {"preserve_raw": True}),
        ("inspection", "canonical_key", "string", 1, 0, None, {"column": "semantic_object_instance.canonical_key"}, {"non_empty": True}),
        ("inspection", "source_snapshot_id", "string", 1, 0, None, {"column": "semantic_object_instance.source_snapshot_id"}, {"non_empty": True}),
        ("inspection", "subject_device", "reference", 1, 0, None, {"relation": "device_has_inspection"}, {"exact_identity_required": True}),
        ("defect", "canonical_key", "string", 1, 0, None, {"column": "semantic_object_instance.canonical_key"}, {"non_empty": True}),
        ("defect", "source_snapshot_id", "string", 1, 0, None, {"column": "semantic_object_instance.source_snapshot_id"}, {"non_empty": True}),
        ("defect", "subject_device", "reference", 1, 0, None, {"relation": "device_has_defect"}, {"exact_identity_required": True}),
        ("work_order", "canonical_key", "string", 1, 0, None, {"column": "semantic_object_instance.canonical_key"}, {"non_empty": True}),
        ("work_order", "source_snapshot_id", "string", 1, 0, None, {"column": "semantic_object_instance.source_snapshot_id"}, {"non_empty": True}),
        ("work_order", "subject_device", "reference", 1, 0, None, {"relation": "device_has_work_order"}, {"exact_identity_required": True}),
        ("rule", "rule_version", "string", 1, 0, None, {"column": "semantic_executable_rule.rule_version"}, {"non_empty": True}),
        ("rule", "condition", "json", 1, 0, None, {"column": "semantic_executable_rule.condition_json"}, {"object": True}),
        ("rule", "action", "json", 1, 0, None, {"column": "semantic_executable_rule.action_json"}, {"object": True}),
    ]
    for object_type, property_key, value_type, required, repeatable, unit, source_mapping, validation in rows:
        db.execute(
            """
            INSERT INTO semantic_object_property(
              property_id,object_type,property_key,value_type,required,repeatable,unit,
              source_mapping_json,validation_json,version,review_status,status,created_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,'approved','active',?,?)
            ON CONFLICT(object_type,property_key,version) DO UPDATE SET
              value_type=excluded.value_type,required=excluded.required,repeatable=excluded.repeatable,
              unit=excluded.unit,source_mapping_json=excluded.source_mapping_json,
              validation_json=excluded.validation_json,review_status='approved',status='active',updated_at=excluded.updated_at
            """,
            (
                sid("SOP", object_type, property_key, CONTRACT_VERSION), object_type, property_key, value_type,
                required, repeatable, unit, json_text(source_mapping), json_text(validation),
                CONTRACT_VERSION, created, created,
            ),
        )

    lifecycle_rows = [
        ("device", "active", 1, 0, ["retired", "replaced", "blocked"]),
        ("device", "retired", 0, 1, []),
        ("inspection", "recorded", 1, 0, ["completed", "cancelled"]),
        ("inspection", "completed", 0, 1, []),
        ("defect", "open", 1, 0, ["in_progress", "closed", "cancelled"]),
        ("defect", "in_progress", 0, 0, ["closed", "cancelled"]),
        ("defect", "closed", 0, 1, []),
        ("work_order", "created", 1, 0, ["in_progress", "completed", "cancelled"]),
        ("work_order", "in_progress", 0, 0, ["completed", "cancelled"]),
        ("work_order", "completed", 0, 1, []),
    ]
    for object_type, state, initial, terminal, allowed in lifecycle_rows:
        db.execute(
            """
            INSERT INTO semantic_object_lifecycle(
              lifecycle_id,object_type,state,initial_state,terminal_state,allowed_next_json,
              version,review_status,status,created_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,'approved','active',?,?)
            ON CONFLICT(object_type,state,version) DO UPDATE SET
              initial_state=excluded.initial_state,terminal_state=excluded.terminal_state,
              allowed_next_json=excluded.allowed_next_json,review_status='approved',status='active',updated_at=excluded.updated_at
            """,
            (sid("SOL", object_type, state, CONTRACT_VERSION), object_type, state, initial, terminal,
             json_text(allowed), CONTRACT_VERSION, created, created),
        )


def seed_event_contracts(db: sqlite3.Connection, created: str) -> None:
    rows = [
        ("inspection_causes_defect", "InspectionEvent", "DefectEvent"),
        ("defect_triggers_work_order", "DefectEvent", "WorkOrderEvent"),
        ("work_order_resolves_defect", "WorkOrderEvent", "DefectEvent"),
        ("inspection_records_defect", "InspectionEvent", "DefectEvent"),
    ]
    for relation_type, subject, object_type in rows:
        predicate = event_predicate(relation_type)
        db.execute(
            """
            INSERT INTO semantic_event_relation_contract(
              contract_id,relation_type,predicate,subject_event_type,object_event_type,
              min_cardinality,max_cardinality,evidence_required,version,review_status,status,created_at,updated_at
            ) VALUES (?,?,?,?,?,0,NULL,1,?,'approved','active',?,?)
            ON CONFLICT(relation_type,version) DO UPDATE SET
              predicate=excluded.predicate,subject_event_type=excluded.subject_event_type,
              object_event_type=excluded.object_event_type,evidence_required=1,
              review_status='approved',status='active',updated_at=excluded.updated_at
            """,
            (sid("SECR", relation_type, CONTRACT_VERSION), relation_type, predicate, subject, object_type,
             CONTRACT_VERSION, created, created),
        )


def seed_executable_rules(db: sqlite3.Connection, created: str) -> None:
    rules: list[dict[str, Any]] = [
        {
            "rule_id": "SGR:identity-exact-unique",
            "rule_version": "SGRV:identity-exact-unique-v1",
            "rule_type": "identity",
            "title": "唯一精确源记录自动映射设备",
            "target": "device",
            "input": {"source_record": True, "identity_assertion": True},
            "condition": {"all": ["accepted_evidence", "confidence >= 0.99", "unique_source_to_canonical", "no_active_conflict"], "key_types": ["ASSETNUM", "ASSETNUM1"]},
            "result": {"mapping_status": "accepted", "decision_mode": "auto", "review_required": False},
            "action": {"type": "materialize_local_identity_assertion", "source_write": False},
            "source_asset": "SIP:device-identity",
            "source_version": "SIPV:device-identity-v1",
            "requires_approval": 1,
        },
        {
            "rule_id": "SGR:identity-conflict-isolation",
            "rule_version": "SGRV:identity-conflict-isolation-v1",
            "rule_type": "identity",
            "title": "身份冲突自动隔离并转人工复核",
            "target": "device",
            "input": {"identity_conflict": True},
            "condition": {"any": ["one_source_record_to_multiple_canonical", "overlapping_temporal_assertions"]},
            "result": {"mapping_status": "isolated", "decision_mode": "manual", "review_required": True},
            "action": {"type": "isolate_identity_conflict", "source_write": False},
            "source_asset": "SIP:device-identity",
            "source_version": "SIPV:device-identity-v1",
            "requires_approval": 1,
        },
        {
            "rule_id": "SGR:business-link-classification",
            "rule_version": "SGRV:business-link-classification-v1",
            "rule_type": "relation",
            "title": "按业务记录类型生成强类型设备关系",
            "target": "BusinessObjectRelation",
            "input": {"business_record_link": True},
            "condition": {"mapping": {"inspection": "device_has_inspection", "defect": "device_has_defect", "work_order": "device_has_work_order"}},
            "result": {"relation_contract_required": True, "unknown_type": "isolate"},
            "action": {"type": "materialize_local_relation_assertion", "source_write": False},
            "source_asset": "business_record_link",
            "source_version": CONTRACT_VERSION,
            "requires_approval": 0,
        },
        {
            "rule_id": "SGR:event-provenance-required",
            "rule_version": "SGRV:event-provenance-required-v1",
            "rule_type": "event",
            "title": "事件必须有来源、主体和快照追溯",
            "target": "BusinessEvent",
            "input": {"semantic_event": True},
            "condition": {"required_fields": ["event_id", "event_type", "subject_key", "source_schema", "source_table", "source_row_id", "source_snapshot_id"]},
            "result": {"missing_provenance": "isolate"},
            "action": {"type": "validate_event_provenance", "source_write": False},
            "source_asset": "semantic_event",
            "source_version": CONTRACT_VERSION,
            "requires_approval": 0,
        },
        {
            "rule_id": "SGR:pending-defect-risk-action",
            "rule_version": "SGRV:pending-defect-risk-action-v1",
            "rule_type": "action",
            "title": "缺陷待处理且存在风险事实时生成待审批行动",
            "target": "defect",
            "input": {"current_state": "PENDING", "fact_type": "risk_assessment"},
            "condition": {"risk_score_gte": 3, "source_write": False},
            "result": {"action_plan": "create_work_order", "approval_required": True},
            "action": {"type": "create_local_action_plan", "external_adapter": "disabled"},
            "source_asset": "SAR:defect-pending-risk-treatment",
            "source_version": "SARV:defect-pending-risk-treatment-v1",
            "requires_approval": 1,
        },
    ]
    for row in db.execute("SELECT * FROM semantic_logic_rule ORDER BY rule_asset_id,rule_version_id").fetchall():
        rules.append({
            "rule_id": str(row["rule_asset_id"]),
            "rule_version": str(row["rule_version_id"]),
            "rule_type": "fact",
            "title": str(row["decision"]),
            "target": "semantic_fact",
            "input": {"fact_type": row["input_fact_type"], "predicate": row["input_predicate"]},
            "condition": {"input_fact_type": row["input_fact_type"], "input_predicate": row["input_predicate"]},
            "result": {"output_fact_type": row["output_fact_type"], "output_predicate": row["output_predicate"], "decision": row["decision"]},
            "action": {"type": "derive_local_fact", "source_write": False},
            "source_asset": row["rule_asset_id"],
            "source_version": row["rule_version_id"],
            "requires_approval": int(row["requires_approval"] or 0),
        })

    for row in rules:
        db.execute(
            """
            INSERT INTO semantic_executable_rule(
              rule_id,rule_version,rule_type,title,target_object_type,input_schema_json,
              condition_json,result_schema_json,action_json,effective_from,effective_to,
              source_asset_id,source_version_id,status,requires_approval,source_write,
              formal_publication,provenance_json,created_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,NULL,?,?,?, ?,0,0,?,?,?)
            ON CONFLICT(rule_id,rule_version) DO UPDATE SET
              rule_type=excluded.rule_type,title=excluded.title,target_object_type=excluded.target_object_type,
              input_schema_json=excluded.input_schema_json,condition_json=excluded.condition_json,
              result_schema_json=excluded.result_schema_json,action_json=excluded.action_json,
              source_asset_id=excluded.source_asset_id,source_version_id=excluded.source_version_id,
              status=CASE WHEN semantic_executable_rule.status IN ('approved','enabled') THEN semantic_executable_rule.status ELSE excluded.status END,
              requires_approval=excluded.requires_approval,provenance_json=excluded.provenance_json,
              updated_at=excluded.updated_at
            """,
            (
                row["rule_id"], row["rule_version"], row["rule_type"], row["title"], row["target"],
                json_text(row["input"]), json_text(row["condition"]), json_text(row["result"]),
                json_text(row["action"]), created, row["source_asset"],
                row["source_version"], "enabled" if row["rule_type"] in {"fact", "action"} else "replayed",
                row["requires_approval"], json_text({"source_asset": row["source_asset"], "source_version": row["source_version"], "contract_version": CONTRACT_VERSION}),
                created, created,
            ),
        )


def classify_identity_assertions(db: sqlite3.Connection, created: str) -> dict[str, int]:
    policy_version = "SIPV:device-identity-v1"
    db.execute("DELETE FROM semantic_identity_conflict")
    conflict_queries = [
        (
            "source_record_to_multiple_canonical",
            """SELECT source_schema,source_table,source_row_id,source_key_type,source_key,
                      count(DISTINCT canonical_object_id) AS canonical_count
                 FROM semantic_identity_assertion
                WHERE lifecycle_status='active' AND status='accepted'
             GROUP BY source_schema,source_table,source_row_id,source_key_type,source_key
               HAVING count(DISTINCT canonical_object_id)>1""",
        ),
        (
            "source_key_to_multiple_canonical",
            """SELECT source_schema,NULL AS source_table,NULL AS source_row_id,source_key_type,source_key,
                      count(DISTINCT canonical_object_id) AS canonical_count
                 FROM semantic_identity_assertion
                WHERE lifecycle_status='active' AND status='accepted'
             GROUP BY source_schema,source_key_type,source_key
               HAVING count(DISTINCT canonical_object_id)>1""",
        ),
    ]
    for conflict_type, query in conflict_queries:
        for group in db.execute(query).fetchall():
            predicates = ["source_schema=?", "source_key_type=?", "source_key=?"]
            params: list[object] = [group["source_schema"], group["source_key_type"], group["source_key"]]
            if group["source_table"] is not None:
                predicates.append("source_table=?")
                params.append(group["source_table"])
            if group["source_row_id"] is not None:
                predicates.append("source_row_id=?")
                params.append(group["source_row_id"])
            rows = db.execute(
                "SELECT assertion_id,canonical_object_id FROM semantic_identity_assertion WHERE " + " AND ".join(predicates) + " ORDER BY assertion_id",
                params,
            ).fetchall()
            canonical_ids = sorted({str(row["canonical_object_id"]) for row in rows})
            assertion_ids = sorted({str(row["assertion_id"]) for row in rows})
            db.execute(
                """
                INSERT INTO semantic_identity_conflict(
                  conflict_id,conflict_type,source_schema,source_table,source_row_id,source_key_type,source_key,
                  canonical_object_ids_json,assertion_ids_json,severity,status,evidence_json,detected_at
                ) VALUES (?,?,?,?,?,?,?,?,?,'high','isolated',?,?)
                ON CONFLICT(conflict_type,source_schema,source_table,source_row_id,source_key_type,source_key)
                DO UPDATE SET canonical_object_ids_json=excluded.canonical_object_ids_json,
                  assertion_ids_json=excluded.assertion_ids_json,status='isolated',evidence_json=excluded.evidence_json,detected_at=excluded.detected_at
                """,
                (
                    sid("SIC", conflict_type, group["source_schema"], group["source_table"], group["source_row_id"], group["source_key_type"], group["source_key"]),
                    conflict_type, group["source_schema"], group["source_table"], group["source_row_id"], group["source_key_type"], group["source_key"],
                    json_text(canonical_ids), json_text(assertion_ids), json_text({"canonical_count": group["canonical_count"], "policy": policy_version}), created,
                ),
            )

    conflict_assertions = set()
    for row in db.execute("SELECT assertion_ids_json FROM semantic_identity_conflict WHERE status='isolated'").fetchall():
        try:
            conflict_assertions.update(json.loads(row[0] or "[]"))
        except (TypeError, json.JSONDecodeError):
            pass

    auto_count = manual_count = derived_count = review_count = 0
    review_states = {
        str(row["assertion_id"]): str(row["queue_status"])
        for row in db.execute("SELECT assertion_id,queue_status FROM semantic_identity_review").fetchall()
    }
    for row in db.execute("SELECT * FROM semantic_identity_assertion ORDER BY assertion_id").fetchall():
        status = str(row["status"])
        confidence = safe_confidence(row["confidence"])
        evidence = parse_json(row["evidence_json"])
        key_type = str(row["source_key_type"])
        prior_review = review_states.get(str(row["assertion_id"]))
        if prior_review in {"approved", "rejected"}:
            mode = "manual"
            review_required = 0
            manual_count += 1
        elif str(row["assertion_id"]) in conflict_assertions:
            mode = "manual"
            review_required = 1
        elif status == "accepted" and confidence >= 0.99 and key_type in {"ASSETNUM", "ASSETNUM1"}:
            mode = "auto"
            review_required = 0
            auto_count += 1
        elif status in {"needs_review", "rejected"} or confidence < 0.99 or not evidence:
            mode = "manual"
            review_required = 1
            manual_count += 1
        else:
            mode = "derived"
            review_required = 0
            derived_count += 1
        if review_required:
            review_count += 1
        db.execute(
            """
            UPDATE semantic_identity_assertion
               SET decision_mode=?,review_required=?,policy_version=?,lifecycle_status=COALESCE(lifecycle_status,'active'),updated_at=?
             WHERE assertion_id=?
            """,
            (mode, review_required, policy_version, created, row["assertion_id"]),
        )
    return {"auto": auto_count, "manual": manual_count, "derived": derived_count, "review_required": review_count,
            "conflicts": int(db.execute("SELECT count(*) FROM semantic_identity_conflict WHERE status='isolated'").fetchone()[0])}


def materialize_relation_assertions(db: sqlite3.Connection, created: str) -> int:
    mapping = {"inspection": "device_has_inspection", "defect": "device_has_defect", "work_order": "device_has_work_order"}
    count = 0
    for row in db.execute("SELECT * FROM business_record_link ORDER BY link_id").fetchall():
        relation_type = mapping.get(str(row["business_type"]))
        if not relation_type or not row["unified_device_id"]:
            continue
        object_key = f"{row['source_schema']}|{row['source_table']}|{row['source_row_id']}"
        evidence = parse_json(row["evidence_json"])
        evidence.update({"business_link_id": row["link_id"], "relation_policy": CONTRACT_VERSION})
        db.execute(
            """
            INSERT INTO semantic_relation_assertion(
              relation_id,relation_type,subject_type,subject_key,object_type,object_key,
              source_schema,source_table,source_row_id,source_snapshot_id,status,confidence,
              evidence_json,created_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(relation_type,subject_type,subject_key,object_type,object_key) DO UPDATE SET
              source_schema=excluded.source_schema,source_table=excluded.source_table,source_row_id=excluded.source_row_id,
              source_snapshot_id=excluded.source_snapshot_id,status=excluded.status,confidence=excluded.confidence,
              evidence_json=excluded.evidence_json,updated_at=excluded.updated_at
            """,
            (
                sid("SRA", relation_type, row["unified_device_id"], object_key), relation_type, "device", row["unified_device_id"],
                row["business_type"], object_key, row["source_schema"], row["source_table"], row["source_row_id"],
                row["source_snapshot_id"], row["status"] if row["status"] in {"accepted", "needs_review", "rejected"} else "needs_review",
                safe_confidence(row["confidence"]), json_text(evidence), created, created,
            ),
        )
        count += 1
    return count


def seed_identity_review_queue(db: sqlite3.Connection, created: str) -> dict[str, int]:
    """Create review rows for policy-required assertions without deciding them."""
    created_count = 0
    for row in db.execute(
        "SELECT assertion_id FROM semantic_identity_assertion WHERE review_required=1 AND lifecycle_status='active' ORDER BY assertion_id"
    ).fetchall():
        review_id = sid("SIR", row["assertion_id"])
        existing = db.execute("SELECT queue_status FROM semantic_identity_review WHERE assertion_id=?", (row["assertion_id"],)).fetchone()
        if existing is None:
            db.execute(
                """
                INSERT INTO semantic_identity_review(
                  review_id,assertion_id,queue_status,review_note,evidence_json,created_at,updated_at
                ) VALUES (?,?, 'pending','待确认身份映射的来源、唯一性或冲突证据','{}',?,?)
                """,
                (review_id, row["assertion_id"], created, created),
            )
            created_count += 1
    return {
        "created": created_count,
        "pending": int(db.execute("SELECT count(*) FROM semantic_identity_review WHERE queue_status='pending'").fetchone()[0]),
        "approved": int(db.execute("SELECT count(*) FROM semantic_identity_review WHERE queue_status='approved'").fetchone()[0]),
        "rejected": int(db.execute("SELECT count(*) FROM semantic_identity_review WHERE queue_status='rejected'").fetchone()[0]),
    }


def quality_check(db: sqlite3.Connection, created: str) -> dict[str, int]:
    db.execute("DELETE FROM semantic_constraint_violation WHERE constraint_key LIKE 'governance_%'")
    issues = 0
    critical = isolated = missing_provenance = orphan_relations = 0

    # Relation assertions must be backed by an active contract with matching domain/range.
    valid_contracts = {
        (row["relation_type"], row["subject_type"], row["object_type"])
        for row in db.execute("SELECT relation_type,subject_type,object_type FROM semantic_relation_contract WHERE status='active' AND review_status='approved'").fetchall()
    }
    for row in db.execute("SELECT * FROM semantic_relation_assertion").fetchall():
        key = (row["relation_type"], row["subject_type"], row["object_type"])
        if key not in valid_contracts:
            orphan_relations += 1
            isolated += 1
            db.execute(
                """
                INSERT INTO semantic_constraint_violation(
                  violation_id,constraint_key,entity_type,entity_id,severity,status,expected_json,actual_json,evidence_json,created_at
                ) VALUES (?,?,?,?,?,'isolated',?,?,?,?)
                ON CONFLICT(constraint_key,entity_type,entity_id) DO UPDATE SET status='isolated',actual_json=excluded.actual_json,evidence_json=excluded.evidence_json,created_at=excluded.created_at
                """,
                (sid("VIOL", "governance_relation_contract", row["relation_id"]), "governance_relation_contract", "semantic_relation_assertion", row["relation_id"], "high",
                 json_text({"relation": "registered_domain_range"}), json_text(dict(row)), json_text({"relation_id": row["relation_id"]}), created),
            )
            issues += 1

    # Every assertion, event and fact must retain a source pointer.
    provenance_queries = [
        ("semantic_identity_assertion", "assertion_id", "source_schema,source_table,source_row_id,source_snapshot_id"),
        ("semantic_relation_assertion", "relation_id", "source_schema,source_table,source_row_id,source_snapshot_id"),
        ("semantic_event", "event_id", "source_schema,source_table,source_row_id,source_snapshot_id"),
        ("semantic_fact", "fact_id", "source_schema,source_table,source_row_id"),
    ]
    for table, id_column, fields in provenance_queries:
        for row in db.execute(f"SELECT {id_column},{fields} FROM {table}").fetchall():
            values = [row[field] for field in fields.split(",")]
            if not all(str(value or "").strip() for value in values):
                missing_provenance += 1
                isolated += 1
                db.execute(
                    """
                    INSERT INTO semantic_constraint_violation(
                      violation_id,constraint_key,entity_type,entity_id,severity,status,expected_json,actual_json,evidence_json,created_at
                    ) VALUES (?,?,?,?,?,'isolated',?,?,?,?)
                    ON CONFLICT(constraint_key,entity_type,entity_id) DO UPDATE SET status='isolated',actual_json=excluded.actual_json,evidence_json=excluded.evidence_json,created_at=excluded.created_at
                    """,
                    (sid("VIOL", "governance_provenance", table, row[id_column]), "governance_provenance", table, row[id_column], "high",
                     json_text({"required": fields.split(",")}), json_text({field: row[field] for field in fields.split(",")}), json_text({"entity": row[id_column]}), created),
                )
                issues += 1

    # Preserve gaps instead of fabricating location or event causality.
    device_count = int(db.execute("SELECT count(*) FROM semantic_object_instance WHERE object_type='device' AND status='accepted'").fetchone()[0])
    location_relation_count = int(db.execute("SELECT count(*) FROM semantic_relation_assertion WHERE relation_type='device_located_at' AND status='accepted'").fetchone()[0])
    location_gap = device_count if device_count and location_relation_count == 0 else 0
    if location_gap:
        isolated += 1
        db.execute(
            """
            INSERT INTO semantic_constraint_violation(
              violation_id,constraint_key,entity_type,entity_id,severity,status,expected_json,actual_json,evidence_json,created_at
            ) VALUES (?,?,?,?,?,'isolated',?,?,?,?)
            ON CONFLICT(constraint_key,entity_type,entity_id) DO UPDATE SET status='isolated',actual_json=excluded.actual_json,evidence_json=excluded.evidence_json,created_at=excluded.created_at
            """,
            (sid("VIOL", "governance_location_evidence", CONTRACT_VERSION), "governance_location_evidence", "semantic_object_instance", "device-scope", "medium",
             json_text({"expected": "device_located_at evidence when location is available"}), json_text({"device_count": device_count, "location_relation_count": location_relation_count}),
             json_text({"action": "do_not_infer_location", "source_write": False}), created),
        )
        issues += 1

    event_count = int(db.execute("SELECT count(*) FROM semantic_event WHERE status='accepted'").fetchone()[0])
    causal_count = int(db.execute("SELECT count(*) FROM semantic_event_relation WHERE status='accepted'").fetchone()[0])
    causal_gap = event_count if event_count and causal_count == 0 else 0
    if causal_gap:
        isolated += 1
        db.execute(
            """
            INSERT INTO semantic_constraint_violation(
              violation_id,constraint_key,entity_type,entity_id,severity,status,expected_json,actual_json,evidence_json,created_at
            ) VALUES (?,?,?,?,?,'isolated',?,?,?,?)
            ON CONFLICT(constraint_key,entity_type,entity_id) DO UPDATE SET status='isolated',actual_json=excluded.actual_json,evidence_json=excluded.evidence_json,created_at=excluded.created_at
            """,
            (sid("VIOL", "governance_causal_evidence", CONTRACT_VERSION), "governance_causal_evidence", "semantic_event", "event-scope", "medium",
             json_text({"expected": "explicit cross-event evidence before causal assertion"}), json_text({"event_count": event_count, "causal_relation_count": causal_count}),
             json_text({"action": "register_contract_only", "auto_infer": False, "source_write": False}), created),
        )
        issues += 1

    critical = int(db.execute(
        "SELECT count(*) FROM semantic_constraint_violation WHERE severity='critical' AND status='isolated'"
    ).fetchone()[0]) if db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='semantic_constraint_violation'"
    ).fetchone() else 0

    return {
        "issues": issues,
        "critical": critical,
        "isolated": isolated,
        "missing_provenance": missing_provenance,
        "orphan_relations": orphan_relations,
        "location_gap": location_gap,
        "causal_gap": causal_gap,
    }


def build(target_path: pathlib.Path) -> dict[str, Any]:
    db = connect_local(target_path, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout=30000")
    ensure_schema(db)
    created = now()
    seed_identity_policy(db, created)
    seed_relation_contracts(db, created)
    seed_object_schema(db, created)
    seed_event_contracts(db, created)
    seed_executable_rules(db, created)
    identity = classify_identity_assertions(db, created)
    review_queue = seed_identity_review_queue(db, created)
    relation_assertion_count = materialize_relation_assertions(db, created)
    quality = quality_check(db, created)

    object_schema_count = int(db.execute("SELECT count(*) FROM semantic_object_property WHERE status='active'").fetchone()[0])
    relation_contract_count = int(db.execute("SELECT count(*) FROM semantic_relation_contract WHERE status='active'").fetchone()[0])
    identity_conflict_count = int(db.execute("SELECT count(*) FROM semantic_identity_conflict WHERE status='isolated'").fetchone()[0])
    event_relation_count = int(db.execute("SELECT count(*) FROM semantic_event_relation WHERE status='accepted'").fetchone()[0])
    executable_rule_count = int(db.execute("SELECT count(*) FROM semantic_executable_rule WHERE status IN ('replayed','approved','enabled')").fetchone()[0])
    status = "completed_with_isolation" if quality["isolated"] or identity_conflict_count else "completed"
    run_id = sid("SGQR", CONTRACT_VERSION, created)
    db.execute(
        """
        INSERT INTO semantic_governance_quality_run(
          run_id,contract_version,object_schema_count,relation_contract_count,relation_assertion_count,
          identity_conflict_count,event_relation_count,executable_rule_count,orphan_relation_count,
          location_evidence_gap_count,causal_evidence_gap_count,missing_provenance_count,critical_count,
          isolated_count,status,source_write,formal_publication,created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,0,?)
        """,
        (run_id, CONTRACT_VERSION, object_schema_count, relation_contract_count, relation_assertion_count,
         identity_conflict_count, event_relation_count, executable_rule_count, quality["orphan_relations"],
         quality["location_gap"], quality["causal_gap"], quality["missing_provenance"], quality["critical"],
         quality["isolated"], status, created),
    )
    db.commit()
    result = {
        "run_id": run_id,
        "contract_version": CONTRACT_VERSION,
        "identity": identity,
        "review_queue": review_queue,
        "object_schema_count": object_schema_count,
        "relation_contract_count": relation_contract_count,
        "relation_assertion_count": relation_assertion_count,
        "identity_conflict_count": identity_conflict_count,
        "event_relation_count": event_relation_count,
        "executable_rule_count": executable_rule_count,
        "quality": quality,
        "status": status,
        "source_write": False,
        "formal_publication": False,
    }
    db.close()
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the local semantic governance contract")
    parser.add_argument("--target-db", type=pathlib.Path, default=DEFAULT_TARGET)
    args = parser.parse_args()
    print(json.dumps(build(args.target_db.resolve()), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
