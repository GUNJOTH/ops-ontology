"""Materialize the local Decision -> ActionPlan -> Approval boundary.

This layer consumes already materialized rule decisions and explicit action
recommendations.  It never invents an operational action from a fact and it
never writes an upstream source system.  A decision that says it requires an
action but has no explicit action recommendation is recorded as unbound and
stays out of the approval queue.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import pathlib
import sqlite3
from datetime import datetime, timezone


ROOT = pathlib.Path(__file__).resolve().parent
DEFAULT_TARGET = ROOT / "data" / "unified_semantics.sqlite3"

# Bootstrap data is written once into the local registry.  Evaluation below
# always reads enabled rows back from SQLite so adding a rule does not require
# changing the executor.
DEFAULT_ACTION_RULES: tuple[dict[str, object], ...] = (
    {
        "rule_id": "SAR:defect-pending-risk-treatment",
        "rule_version": "SARV:defect-pending-risk-treatment-v1",
        "title": "缺陷待处理且有风险证据时生成处理建议",
        "input_state": "PENDING",
        "risk_fact_type": "risk_assessment",
        "risk_predicate": "has_risk_assessment",
        "min_risk_score": 3,
        "action_type": "create_work_order",
        "target_type": "device",
        "risk_level": "high",
        "requires_approval": 1,
    },
)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sid(prefix: str, *parts: object) -> str:
    raw = "|".join("" if part is None else str(part) for part in parts)
    return f"{prefix}-{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:24]}"


def safe_number(value: object) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def ensure_schema(db: sqlite3.Connection) -> None:
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS semantic_action_rule (
          rule_id TEXT PRIMARY KEY,
          rule_version TEXT NOT NULL,
          title TEXT NOT NULL,
          input_state TEXT NOT NULL,
          risk_fact_type TEXT NOT NULL,
          risk_predicate TEXT NOT NULL,
          min_risk_score REAL NOT NULL,
          action_type TEXT NOT NULL,
          target_type TEXT NOT NULL,
          risk_level TEXT NOT NULL CHECK (risk_level IN ('low','medium','high')),
          requires_approval INTEGER NOT NULL CHECK (requires_approval IN (0,1)),
          status TEXT NOT NULL CHECK (status IN ('enabled','disabled','blocked')),
          source_write INTEGER NOT NULL CHECK (source_write=0),
          formal_publication INTEGER NOT NULL CHECK (formal_publication=0),
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          UNIQUE(rule_id,rule_version)
        );
        CREATE INDEX IF NOT EXISTS ix_semantic_action_rule_status
          ON semantic_action_rule(status,updated_at);
        CREATE TABLE IF NOT EXISTS semantic_action_plan (
          plan_id TEXT PRIMARY KEY,
          decision_id TEXT NOT NULL REFERENCES semantic_rule_decision(decision_id),
          action_id TEXT,
          action_key TEXT NOT NULL,
          action_type TEXT NOT NULL,
          target_type TEXT NOT NULL,
          target_key TEXT,
          payload_json TEXT NOT NULL,
          reason TEXT NOT NULL,
          risk_level TEXT NOT NULL CHECK (risk_level IN ('low','medium','high')),
          requires_approval INTEGER NOT NULL CHECK (requires_approval IN (0,1)),
          status TEXT NOT NULL CHECK (status IN ('DRAFT','PENDING_APPROVAL','APPROVED','REJECTED','CANCELLED')),
          idempotency_key TEXT NOT NULL UNIQUE,
          source_write INTEGER NOT NULL CHECK (source_write=0),
          formal_publication INTEGER NOT NULL CHECK (formal_publication=0),
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_semantic_action_plan_status
          ON semantic_action_plan(status,updated_at);
        CREATE INDEX IF NOT EXISTS ix_semantic_action_plan_decision
          ON semantic_action_plan(decision_id);
        CREATE TABLE IF NOT EXISTS semantic_action_approval (
          approval_id TEXT PRIMARY KEY,
          plan_id TEXT NOT NULL UNIQUE REFERENCES semantic_action_plan(plan_id),
          status TEXT NOT NULL CHECK (status IN ('PENDING','APPROVED','REJECTED')),
          reviewer TEXT,
          comment TEXT NOT NULL DEFAULT '',
          approval_receipt TEXT,
          requested_at TEXT NOT NULL,
          reviewed_at TEXT,
          source_write INTEGER NOT NULL CHECK (source_write=0),
          formal_publication INTEGER NOT NULL CHECK (formal_publication=0)
        );
        CREATE INDEX IF NOT EXISTS ix_semantic_action_approval_status
          ON semantic_action_approval(status,requested_at);
        CREATE TABLE IF NOT EXISTS semantic_decision_layer_run (
          run_id TEXT PRIMARY KEY,
          input_fact_count INTEGER NOT NULL,
          decision_count INTEGER NOT NULL,
          action_plan_count INTEGER NOT NULL,
          pending_approval_count INTEGER NOT NULL,
          unbound_action_decision_count INTEGER NOT NULL,
          action_rule_count INTEGER NOT NULL DEFAULT 0,
          state_match_count INTEGER NOT NULL DEFAULT 0,
          risk_evidence_count INTEGER NOT NULL DEFAULT 0,
          action_rule_match_count INTEGER NOT NULL DEFAULT 0,
          status TEXT NOT NULL CHECK (status IN ('completed','needs_review','blocked')),
          source_write INTEGER NOT NULL CHECK (source_write=0),
          formal_publication INTEGER NOT NULL CHECK (formal_publication=0),
          created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_semantic_decision_layer_run_created
          ON semantic_decision_layer_run(created_at);
        """
    )
    columns = {row[1] for row in db.execute("PRAGMA table_info(semantic_decision_layer_run)").fetchall()}
    additive = {
        "action_rule_count": "INTEGER NOT NULL DEFAULT 0",
        "state_match_count": "INTEGER NOT NULL DEFAULT 0",
        "risk_evidence_count": "INTEGER NOT NULL DEFAULT 0",
        "action_rule_match_count": "INTEGER NOT NULL DEFAULT 0",
    }
    for column, definition in additive.items():
        if column not in columns:
            db.execute(f"ALTER TABLE semantic_decision_layer_run ADD COLUMN {column} {definition}")


def plan_status(action_status: str, requires_approval: bool) -> str:
    if action_status == "approved":
        return "APPROVED"
    if action_status == "blocked":
        return "DRAFT"
    return "PENDING_APPROVAL" if requires_approval else "DRAFT"


def register_action_rules(db: sqlite3.Connection, created: str) -> None:
    for rule in DEFAULT_ACTION_RULES:
        db.execute(
            """
            INSERT INTO semantic_action_rule(
              rule_id,rule_version,title,input_state,risk_fact_type,risk_predicate,
              min_risk_score,action_type,target_type,risk_level,requires_approval,
              status,source_write,formal_publication,created_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,'enabled',0,0,?,?)
            ON CONFLICT(rule_id,rule_version) DO UPDATE SET
              title=excluded.title,input_state=excluded.input_state,
              risk_fact_type=excluded.risk_fact_type,risk_predicate=excluded.risk_predicate,
              min_risk_score=excluded.min_risk_score,action_type=excluded.action_type,
              target_type=excluded.target_type,risk_level=excluded.risk_level,
              requires_approval=excluded.requires_approval,status='enabled',updated_at=excluded.updated_at
            """,
            (
                rule["rule_id"], rule["rule_version"], rule["title"], rule["input_state"],
                rule["risk_fact_type"], rule["risk_predicate"], rule["min_risk_score"],
                rule["action_type"], rule["target_type"], rule["risk_level"],
                rule["requires_approval"], created, created,
            ),
        )


def evaluate_action_rules(db: sqlite3.Connection, created: str) -> dict[str, int]:
    """Replay explicit action rules without treating missing risk as risk."""
    register_action_rules(db, created)
    action_rules = [dict(row) for row in db.execute(
        "SELECT * FROM semantic_action_rule WHERE status='enabled' ORDER BY rule_id,rule_version"
    ).fetchall()]
    matched = 0
    risk_evidence = 0
    for rule in action_rules:
        current_states = db.execute(
            """
            SELECT * FROM semantic_current_state
            WHERE state_domain='DEFECT' AND current_state=? AND status='current'
            ORDER BY subject_key
            """,
            (rule["input_state"],),
        ).fetchall()
        for state in current_states:
            risk_facts = db.execute(
                """
                SELECT * FROM semantic_fact
                WHERE subject_type=? AND subject_key=? AND fact_type=? AND predicate=?
                  AND status IN ('observed','accepted','derived')
                ORDER BY created_at DESC
                """,
                (state["subject_type"], state["subject_key"], rule["risk_fact_type"], rule["risk_predicate"]),
            ).fetchall()
            valid_risk = None
            for fact in risk_facts:
                try:
                    value = json.loads(fact["value_json"] or "{}")
                except (TypeError, json.JSONDecodeError):
                    value = {}
                risk_score = safe_number(value.get("risk_score")) if isinstance(value, dict) else None
                minimum_score = safe_number(rule["min_risk_score"])
                if risk_score is not None and minimum_score is not None and risk_score >= minimum_score:
                    valid_risk = fact
                    break
            if valid_risk is None:
                continue
            risk_evidence += 1
            matched += 1
            decision_id = sid("SRD", rule["rule_version"], state["subject_type"], state["subject_key"], valid_risk["fact_id"])
            action_key = sid("SACT", rule["rule_version"], state["subject_key"], valid_risk["fact_id"])
            context = {
                "state": state["current_state"],
                "stateFactId": state["source_fact_id"],
                "riskFactId": valid_risk["fact_id"],
                "riskScore": json.loads(valid_risk["value_json"] or "{}").get("risk_score"),
                "sourceWrite": False,
                "formalPublication": False,
            }
            db.execute(
                """
                INSERT OR IGNORE INTO semantic_rule_decision(
                  decision_id,subject_type,subject_key,rule_asset_id,rule_version_id,
                  input_fact_ids_json,input_context_json,decision,confidence,explanation,
                  status,requires_action,created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?, 'accepted',1,?)
                """,
                (
                    decision_id, state["subject_type"], state["subject_key"], rule["rule_id"],
                    rule["rule_version"], json.dumps([state["source_fact_id"], valid_risk["fact_id"]]),
                    json.dumps(context, ensure_ascii=False), "pending_defect_requires_treatment", 1.0,
                    "缺陷当前状态为 PENDING，且存在达到阈值的风险事实；仅生成待审批处理建议。", created,
                ),
            )
            payload = {
                "ruleId": rule["rule_id"],
                "ruleVersion": rule["rule_version"],
                "stateFactId": state["source_fact_id"],
                "riskFactId": valid_risk["fact_id"],
                "riskScore": context["riskScore"],
                "recommendation": "建议创建处理工单；需人工审批后才可执行",
                "sourceWrite": False,
                "formalPublication": False,
            }
            db.execute(
                """
                INSERT OR IGNORE INTO semantic_escalation_action(
                  action_id,decision_id,action_type,target_type,target_key,payload_json,
                  status,requires_approval,idempotency_key,source_write,created_at
                ) VALUES (?,?,?,?,?,?, 'planned',1,?,0,?)
                """,
                (
                    action_key, decision_id, rule["action_type"], rule["target_type"],
                    state["subject_key"], json.dumps(payload, ensure_ascii=False), action_key, created,
                ),
            )
    return {"action_rule_count": len(action_rules), "state_match_count": sum(
        int(db.execute("SELECT count(*) FROM semantic_current_state WHERE state_domain='DEFECT' AND current_state=? AND status='current'", (rule["input_state"],)).fetchone()[0])
        for rule in action_rules
    ), "risk_evidence_count": risk_evidence, "action_rule_match_count": matched}


def build(target_path: pathlib.Path) -> dict[str, object]:
    db = sqlite3.connect(str(target_path), timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA busy_timeout=30000")
    ensure_schema(db)
    created = now()
    action_rule_counts = evaluate_action_rules(db, created)

    decisions = db.execute(
        """
        SELECT * FROM semantic_rule_decision
        WHERE status IN ('accepted','proposed','needs_review')
        ORDER BY decision_id
        """
    ).fetchall()
    fact_ids: set[str] = set()
    for decision in decisions:
        try:
            fact_ids.update(str(item) for item in json.loads(decision["input_fact_ids_json"] or "[]"))
        except (TypeError, json.JSONDecodeError):
            continue

    explicit_actions = db.execute(
        """
        SELECT a.*,d.subject_type,d.subject_key,d.explanation,d.confidence,d.rule_asset_id,d.rule_version_id
        FROM semantic_escalation_action a
        JOIN semantic_rule_decision d ON d.decision_id=a.decision_id
        WHERE d.status IN ('accepted','proposed','needs_review')
        ORDER BY a.action_id
        """
    ).fetchall()
    action_ids = {str(row["action_id"]) for row in explicit_actions}
    created_plans = 0
    for action in explicit_actions:
        requires_approval = bool(action["requires_approval"])
        plan_id = sid("SAP", action["idempotency_key"])
        payload = {
            "sourceActionId": action["action_id"],
            "decisionId": action["decision_id"],
            "subjectType": action["subject_type"],
            "subjectKey": action["subject_key"],
            "sourcePayload": json.loads(action["payload_json"] or "{}"),
            "ruleAssetId": action["rule_asset_id"],
            "ruleVersionId": action["rule_version_id"],
            "confidence": action["confidence"],
            "sourceWrite": False,
            "formalPublication": False,
        }
        status = plan_status(str(action["status"]), requires_approval)
        plan_exists = db.execute("SELECT 1 FROM semantic_action_plan WHERE idempotency_key=?", (action["idempotency_key"],)).fetchone() is not None
        cursor = db.execute(
            """
            INSERT INTO semantic_action_plan(
              plan_id,decision_id,action_id,action_key,action_type,target_type,target_key,
              payload_json,reason,risk_level,requires_approval,status,idempotency_key,
              source_write,formal_publication,created_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,0,0,?,?)
            ON CONFLICT(idempotency_key) DO UPDATE SET
              payload_json=excluded.payload_json,
              reason=excluded.reason,
              status=CASE
                WHEN semantic_action_plan.status IN ('APPROVED','REJECTED','CANCELLED')
                THEN semantic_action_plan.status ELSE excluded.status END,
              updated_at=excluded.updated_at
            """,
            (
                plan_id, action["decision_id"], action["action_id"],
                f"{action['action_type']}:{action['target_type']}", action["action_type"],
                action["target_type"], action["target_key"], json.dumps(payload, ensure_ascii=False),
                action["payload_json"], "high" if requires_approval else "medium", int(requires_approval),
                status, action["idempotency_key"], created, created,
            ),
        )
        if requires_approval:
            plan = db.execute("SELECT status FROM semantic_action_plan WHERE plan_id=?", (plan_id,)).fetchone()
            if plan and plan["status"] == "PENDING_APPROVAL":
                db.execute(
                    """
                    INSERT OR IGNORE INTO semantic_action_approval(
                      approval_id,plan_id,status,comment,requested_at,source_write,formal_publication
                    ) VALUES (?,?,'PENDING','',?,0,0)
                    """,
                    (sid("SAA", plan_id), plan_id, created),
                )
        created_plans += int(not plan_exists)

    unbound = int(db.execute(
        """
        SELECT count(*) FROM semantic_rule_decision d
        WHERE d.status IN ('accepted','proposed','needs_review')
          AND d.requires_action=1
          AND NOT EXISTS (SELECT 1 FROM semantic_escalation_action a WHERE a.decision_id=d.decision_id)
        """
    ).fetchone()[0])
    pending = int(db.execute("SELECT count(*) FROM semantic_action_plan WHERE status='PENDING_APPROVAL'").fetchone()[0])
    total_plans = int(db.execute("SELECT count(*) FROM semantic_action_plan").fetchone()[0])
    run_status = "needs_review" if unbound else "completed"
    run_id = sid("SDAL", created)
    db.execute(
        """
        INSERT INTO semantic_decision_layer_run(
          run_id,input_fact_count,decision_count,action_plan_count,pending_approval_count,
          unbound_action_decision_count,action_rule_count,state_match_count,risk_evidence_count,
          action_rule_match_count,status,source_write,formal_publication,created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,0,0,?)
        """,
        (
            run_id, len(fact_ids), len(decisions), total_plans, pending, unbound,
            action_rule_counts["action_rule_count"], action_rule_counts["state_match_count"],
            action_rule_counts["risk_evidence_count"], action_rule_counts["action_rule_match_count"],
            run_status, created,
        ),
    )
    db.commit()
    db.close()
    return {
        "run_id": run_id,
        "input_fact_count": len(fact_ids),
        "decision_count": len(decisions),
        "action_plan_count": total_plans,
        "created_action_plan_count": created_plans,
        "pending_approval_count": pending,
        "unbound_action_decision_count": unbound,
        "status": run_status,
        "source_write": False,
        "formal_publication": False,
        "note": "没有明确行动证据的规则判断不会被自动转成工单或源系统写入。",
        **action_rule_counts,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the local decision/action/approval layer")
    parser.add_argument("--target-db", type=pathlib.Path, default=DEFAULT_TARGET)
    args = parser.parse_args()
    print(json.dumps(build(args.target_db.resolve()), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
