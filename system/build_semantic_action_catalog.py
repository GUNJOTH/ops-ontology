"""Build the first-class business Action catalog.

The catalog turns the book/design concept of "Action" into a machine-readable
local asset:

    Action = business meaning + allowed-when + required input facts
             + permission scope + adapter mappings + effects
             + execution state machine

It does not grant execution rights.  Every operational action remains behind
approval, idempotency, source-write and formal-publication gates.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import sqlite3
from datetime import datetime, timezone

from pipeline.contracts import connect_local

ROOT = pathlib.Path(__file__).resolve().parent
DEFAULT_TARGET = ROOT / "data" / "unified_semantics.sqlite3"


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sid(prefix: str, *parts: object) -> str:
    raw = "|".join("" if part is None else str(part) for part in parts)
    return f"{prefix}-{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:24]}"


DEFAULT_ACTION_CATALOG: tuple[dict[str, object], ...] = (
    {
        "action_id": "ACTION_CREATE_DEFECT",
        "action_key": "create_defect",
        "action_name": "创建缺陷",
        "business_meaning": "将已经确认需要升级处理的设备异常转入正式缺陷管理流程。",
        "target_type": "device",
        "allowed_when": {
            "abnormal_fact_exists": True,
            "abnormal_reaches_defect_conversion_condition": True,
            "equipment_identity_confirmed": True,
            "human_approval_complete": True,
        },
        "required_facts": ["equipment", "abnormal_description", "risk_level", "evidence"],
        "permission_scope": {
            "actors": ["defect_system_operator", "governed_agent"],
            "policy": "Agent only proposes; a human approval is required before execution.",
        },
        "adapter_mappings": [
            {"kind": "mcp", "name": "eam_create_defect", "system": "EAM", "enabled": False},
            {"kind": "api", "name": "/api/v1/defects", "system": "DEFECT", "enabled": False},
        ],
        "effects": {
            "creates": "Defect",
            "new_facts": [
                {"fact_type": "defect_status", "value": "待处理"},
                {"fact_type": "equipment_has_active_defect", "value": True},
            ],
        },
        "execution_states": ["requested", "executing", "succeeded", "failed"],
        "requires_approval": 1,
    },
    {
        "action_id": "ACTION_CREATE_WORK_ORDER",
        "action_key": "create_work_order",
        "action_name": "创建维修工单",
        "business_meaning": "将已批准处理的缺陷或风险转成正式维修工单。",
        "target_type": "defect",
        "allowed_when": {
            "defect_status_approved": True,
            "review_approved": True,
            "team_assignment_present": True,
        },
        "required_facts": ["defect", "work_order_type", "priority", "evidence"],
        "permission_scope": {
            "actors": ["work_order_system_operator", "governed_agent"],
            "policy": "External work-order creation must be separately approved.",
        },
        "adapter_mappings": [
            {"kind": "mcp", "name": "eam_create_work_order", "system": "EAM", "enabled": False},
            {"kind": "workflow", "name": "work_order_creation_workflow", "system": "WORK_ORDER", "enabled": False},
        ],
        "effects": {
            "creates": "WorkOrder",
            "new_facts": [
                {"fact_type": "work_order_status", "value": "created"},
                {"fact_type": "defect_has_active_work_order", "value": True},
            ],
        },
        "execution_states": ["requested", "executing", "succeeded", "failed"],
        "requires_approval": 1,
    },
    {
        "action_id": "ACTION_STOP_PUMP",
        "action_key": "stop_pump",
        "action_name": "停止循环泵",
        "business_meaning": "在满足安全前置条件、操作权限和人工审批后停止设备。",
        "target_type": "device",
        "allowed_when": {
            "equipment_running": True,
            "backup_pump_online": True,
            "operation_permission_granted": True,
            "human_approval_complete": True,
        },
        "required_facts": ["equipment_status", "backup_pump_status", "operation_permission", "approval_receipt"],
        "permission_scope": {
            "actors": ["operations_center", "dcs_authorized_operator"],
            "policy": "No agent may stop equipment without operation permission and human approval.",
        },
        "adapter_mappings": [
            {"kind": "mcp", "name": "dcs_stop_pump", "system": "DCS", "enabled": False},
            {"kind": "workflow", "name": "operations_stop_workflow", "system": "OPS", "enabled": False},
        ],
        "effects": {
            "changes": ["equipment_status"],
            "new_facts": [
                {"fact_type": "equipment_status", "value": "stopped"},
                {"fact_type": "pump_running", "value": False},
            ],
        },
        "execution_states": ["requested", "executing", "succeeded", "failed"],
        "requires_approval": 1,
    },
    {
        "action_id": "ACTION_REQUEST_HUMAN_APPROVAL",
        "action_key": "request_human_approval",
        "action_name": "发起人工审批",
        "business_meaning": "为高风险决策创建待人工审批任务，不自动写入审批通过结果。",
        "target_type": "decision",
        "allowed_when": {
            "high_risk_decision": True,
            "requires_human_review": True,
        },
        "required_facts": ["decision", "risk_level", "evidence"],
        "permission_scope": {
            "actors": ["governance_system", "approval_system"],
            "policy": "This action only creates an approval request; it never sets approved=true.",
        },
        "adapter_mappings": [
            {"kind": "workflow", "name": "approval_workflow", "system": "OA", "enabled": False},
            {"kind": "api", "name": "/api/v1/approval-requests", "system": "GOVERNANCE", "enabled": False},
        ],
        "effects": {
            "creates": "ApprovalRequest",
            "new_facts": [
                {"fact_type": "approval_request_created", "value": True},
                {"fact_type": "approval_status", "value": "pending"},
            ],
        },
        "execution_states": ["requested", "executing", "succeeded", "failed"],
        "requires_approval": 0,
    },
)


def ensure_schema(db: sqlite3.Connection) -> None:
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS semantic_action_definition (
          action_id TEXT PRIMARY KEY,
          action_key TEXT NOT NULL,
          action_name TEXT NOT NULL,
          business_meaning TEXT NOT NULL,
          target_type TEXT NOT NULL,
          allowed_when_json TEXT NOT NULL,
          required_facts_json TEXT NOT NULL,
          permission_scope_json TEXT NOT NULL,
          adapter_mappings_json TEXT NOT NULL,
          effects_json TEXT NOT NULL,
          execution_states_json TEXT NOT NULL,
          requires_approval INTEGER NOT NULL CHECK (requires_approval IN (0,1)),
          source_write INTEGER NOT NULL CHECK (source_write=0),
          formal_publication INTEGER NOT NULL CHECK (formal_publication=0),
          status TEXT NOT NULL CHECK (status IN ('draft','ready','enabled','blocked','retired')),
          version TEXT NOT NULL,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          UNIQUE(action_key,version)
        );
        CREATE INDEX IF NOT EXISTS ix_semantic_action_definition_status
          ON semantic_action_definition(status,action_key);
        CREATE TABLE IF NOT EXISTS semantic_action_catalog_run (
          run_id TEXT PRIMARY KEY,
          action_count INTEGER NOT NULL,
          enabled_count INTEGER NOT NULL,
          source_write INTEGER NOT NULL CHECK (source_write=0),
          formal_publication INTEGER NOT NULL CHECK (formal_publication=0),
          created_at TEXT NOT NULL
        );
        """
    )


def build(target_path: pathlib.Path) -> dict[str, object]:
    db = connect_local(target_path, timeout=30)
    db.execute("PRAGMA busy_timeout=30000")
    ensure_schema(db)
    created = now()
    for action in DEFAULT_ACTION_CATALOG:
        db.execute(
            """
            INSERT INTO semantic_action_definition(
              action_id,action_key,action_name,business_meaning,target_type,
              allowed_when_json,required_facts_json,permission_scope_json,
              adapter_mappings_json,effects_json,execution_states_json,
              requires_approval,source_write,formal_publication,status,version,
              created_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0,0,'ready',?,?,?)
            ON CONFLICT(action_key,version) DO UPDATE SET
              action_id=excluded.action_id,
              action_name=excluded.action_name,
              business_meaning=excluded.business_meaning,
              target_type=excluded.target_type,
              allowed_when_json=excluded.allowed_when_json,
              required_facts_json=excluded.required_facts_json,
              permission_scope_json=excluded.permission_scope_json,
              adapter_mappings_json=excluded.adapter_mappings_json,
              effects_json=excluded.effects_json,
              execution_states_json=excluded.execution_states_json,
              requires_approval=excluded.requires_approval,
              status='ready',
              updated_at=excluded.updated_at
            """,
            (
                action["action_id"], action["action_key"], action["action_name"],
                action["business_meaning"], action["target_type"],
                json.dumps(action["allowed_when"], ensure_ascii=False, sort_keys=True),
                json.dumps(action["required_facts"], ensure_ascii=False, sort_keys=True),
                json.dumps(action["permission_scope"], ensure_ascii=False, sort_keys=True),
                json.dumps(action["adapter_mappings"], ensure_ascii=False, sort_keys=True),
                json.dumps(action["effects"], ensure_ascii=False, sort_keys=True),
                json.dumps(action["execution_states"], ensure_ascii=False),
                int(action["requires_approval"]), "semantic-action-catalog-v1", created, created,
            ),
        )
    db.commit()
    action_count = int(db.execute("SELECT count(*) FROM semantic_action_definition").fetchone()[0])
    enabled_count = int(db.execute("SELECT count(*) FROM semantic_action_definition WHERE status='enabled'").fetchone()[0])
    unsafe_count = int(db.execute(
        "SELECT count(*) FROM semantic_action_definition WHERE source_write<>0 OR formal_publication<>0"
    ).fetchone()[0])
    run_id = sid("SAC", created)
    db.execute(
        """INSERT INTO semantic_action_catalog_run(run_id,action_count,enabled_count,source_write,formal_publication,created_at)
           VALUES (?,?,?,0,0,?)""",
        (run_id, action_count, enabled_count, created),
    )
    db.commit()
    db.close()
    return {
        "run_id": run_id,
        "action_count": action_count,
        "enabled_count": enabled_count,
        "status": "ready" if unsafe_count == 0 else "blocked",
        "source_write": False,
        "formal_publication": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the local semantic Action catalog")
    parser.add_argument("--target-db", type=pathlib.Path, default=DEFAULT_TARGET)
    args = parser.parse_args()
    print(json.dumps(build(args.target_db.resolve()), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
