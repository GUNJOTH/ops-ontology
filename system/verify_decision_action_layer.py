"""Verify Fact -> Decision -> ActionPlan -> Approval in a temporary overlay.

The current formal snapshot intentionally has no eligible ``PENDING`` defect
with a risk score at the configured threshold.  This verifier therefore uses
an explicitly labelled local fixture in a cloned SQLite database.  It never
changes the formal database or an upstream source system.
"""
from __future__ import annotations

import json
import pathlib
import sqlite3
import sys

from common import sha256_file as digest
from common import sid
from common import utc_now as now
from pipeline.contracts import connect_local, connect_readonly

ROOT = pathlib.Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parent
SOURCE_DB = ROOT / "data" / "unified_semantics.sqlite3"
VERIFY_ROOT = ROOT / "data" / ".verification"
TARGET_DB = VERIFY_ROOT / "decision_action.test.sqlite3"


def clone_source() -> None:
    VERIFY_ROOT.mkdir(parents=True, exist_ok=True)
    TARGET_DB.unlink(missing_ok=True)
    source = connect_readonly(SOURCE_DB.resolve())
    target = connect_local(TARGET_DB)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()


def add_fixture(db: sqlite3.Connection) -> dict[str, str]:
    """Add one fully evidenced, isolated action-chain fixture."""
    created = now()
    subject_type = "device"
    subject_key = "VERIFY-ACTION-DEVICE-001"
    source_schema = "VERIFY_FIXTURE"
    source_table = "ACTION_CHAIN_FIXTURE"
    snapshot = "verify-decision-action-layer-v1"
    state_fact_id = sid("VFACT", "state", subject_key)
    risk_fact_id = sid("VFACT", "risk", subject_key)
    transition_id = sid("VTRANS", subject_key)

    db.execute(
        """
        INSERT INTO semantic_fact(
          fact_id,fact_type,subject_type,subject_key,predicate,value_json,unit,
          source_schema,source_table,source_row_id,source_snapshot_id,status,
          confidence,observed_at,created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            state_fact_id, "defect_status", subject_type, subject_key,
            "canonical_state", json.dumps({"canonical_state": "PENDING"}), None,
            source_schema, source_table, "state-001", snapshot, "observed", 1.0,
            created, created,
        ),
    )
    db.execute(
        """
        INSERT INTO semantic_fact(
          fact_id,fact_type,subject_type,subject_key,predicate,value_json,unit,
          source_schema,source_table,source_row_id,source_snapshot_id,status,
          confidence,observed_at,created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            risk_fact_id, "risk_assessment", subject_type, subject_key,
            "has_risk_assessment", json.dumps({"risk_score": 5, "fixture": True}), None,
            source_schema, source_table, "risk-001", snapshot, "observed", 1.0,
            created, created,
        ),
    )
    db.execute(
        """
        INSERT INTO semantic_state_transition(
          transition_id,subject_type,subject_key,state_domain,from_state,to_state,
          transition_type,trigger_fact_id,event_id,rule_asset_id,rule_version_id,
          effective_at,source_snapshot_id,evidence_json,confidence,status,created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            transition_id, subject_type, subject_key, "DEFECT", None, "PENDING",
            "initial", state_fact_id, None, "VERIFY:state", "VERIFY:state-v1",
            created, snapshot, json.dumps({"fixture": True}), 1.0, "accepted", created,
        ),
    )
    db.execute(
        """
        INSERT INTO semantic_current_state(
          subject_type,subject_key,state_domain,current_state,display_name,
          source_fact_id,transition_id,effective_at,source_snapshot_id,state_version,
          status,updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            subject_type, subject_key, "DEFECT", "PENDING", "验证夹具：缺陷待处理",
            state_fact_id, transition_id, created, snapshot, 1, "current", created,
        ),
    )
    db.commit()
    return {"subject_key": subject_key, "state_fact_id": state_fact_id, "risk_fact_id": risk_fact_id}


def verify() -> dict[str, object]:
    if not SOURCE_DB.exists():
        raise AssertionError(f"正式语义覆盖层不存在: {SOURCE_DB}")
    before = digest(SOURCE_DB)
    clone_source()
    sys.path.insert(0, str(PROJECT_ROOT / "backend"))
    from app import main as backend
    from app.core import db as core_db
    from build_decision_action_layer import build

    try:
        db = connect_local(TARGET_DB)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        fixture = add_fixture(db)
        db.close()

        first = build(TARGET_DB)
        db = connect_local(TARGET_DB)
        db.row_factory = sqlite3.Row
        plan = db.execute(
            "SELECT * FROM semantic_action_plan WHERE target_key=?",
            (fixture["subject_key"],),
        ).fetchone()
        if plan is None:
            raise AssertionError("行动规则未为验证夹具生成 ActionPlan")
        approval = db.execute(
            "SELECT * FROM semantic_action_approval WHERE plan_id=?",
            (plan["plan_id"],),
        ).fetchone()
        if approval is None or approval["status"] != "PENDING":
            raise AssertionError("ActionPlan 未生成待审批凭据")
        db.close()

        backend.UNIFIED_SEMANTICS_DB = TARGET_DB
        core_db.UNIFIED_SEMANTICS_DB = TARGET_DB
        approved = backend.review_semantic_action_plan(
            plan["plan_id"],
            backend.SemanticActionApprovalRequest(
                decision="approved",
                reviewer="verify_decision_action_layer",
                comment="临时隔离夹具回放：仅验证审批链路，不执行行动",
            ),
        )
        second = build(TARGET_DB)

        db = connect_local(TARGET_DB)
        db.row_factory = sqlite3.Row
        try:
            final_plan = db.execute(
                "SELECT * FROM semantic_action_plan WHERE plan_id=?", (plan["plan_id"],)
            ).fetchone()
            final_approval = db.execute(
                "SELECT * FROM semantic_action_approval WHERE plan_id=?", (plan["plan_id"],)
            ).fetchone()
            plan_count = int(db.execute(
                "SELECT count(*) FROM semantic_action_plan WHERE target_key=?",
                (fixture["subject_key"],),
            ).fetchone()[0])
            action_count = int(db.execute(
                "SELECT count(*) FROM semantic_escalation_action WHERE target_key=?",
                (fixture["subject_key"],),
            ).fetchone()[0])
            unsafe = int(db.execute(
                """
                SELECT count(*) FROM semantic_action_plan
                WHERE target_key=? AND (source_write<>0 OR formal_publication<>0)
                """,
                (fixture["subject_key"],),
            ).fetchone()[0])
        finally:
            db.close()

        assert first["status"] == "completed"
        assert int(first["action_rule_match_count"]) >= 1
        assert int(first["created_action_plan_count"]) >= 1
        assert approved["status"] == "APPROVED"
        assert approved["sourceWrite"] is False
        assert approved["formalPublication"] is False
        assert final_plan is not None and final_plan["status"] == "APPROVED"
        assert final_approval is not None and final_approval["status"] == "APPROVED"
        assert final_approval["approval_receipt"]
        assert plan_count == 1
        assert action_count == 1
        assert unsafe == 0
        assert second["status"] == "completed"
        assert digest(SOURCE_DB) == before
        return {
            "status": "passed",
            "fixture": fixture,
            "first_run": first,
            "approved_plan_id": plan["plan_id"],
            "approval_status": final_approval["status"],
            "approval_receipt_present": bool(final_approval["approval_receipt"]),
            "idempotent_plan_count": plan_count,
            "action_count": action_count,
            "source_write": False,
            "formal_publication": False,
            "source_digest_unchanged": True,
        }
    finally:
        backend.UNIFIED_SEMANTICS_DB = SOURCE_DB
        core_db.UNIFIED_SEMANTICS_DB = SOURCE_DB
        TARGET_DB.unlink(missing_ok=True)


if __name__ == "__main__":
    print(json.dumps(verify(), ensure_ascii=False, indent=2))