"""Materialize machine-readable decision, calculation, constraint and action specs.

The specs are derived from the local knowledge-identity layer.  They are
execution contracts, not an auto-publication switch: actions retain approval,
replay and idempotency requirements.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import sqlite3
from datetime import datetime, timezone


ROOT = pathlib.Path(__file__).resolve().parent
DEFAULT_TARGET = ROOT / "data" / "unified_semantics.sqlite3"


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sid(prefix: str, *parts: object) -> str:
    raw = "|".join("" if part is None else str(part) for part in parts)
    return f"{prefix}-{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:24]}"


def json_obj(value: str | None) -> dict[str, object]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else {"value": parsed}
    except (TypeError, json.JSONDecodeError):
        return {"raw": value}


def init_specs(db: sqlite3.Connection) -> None:
    db.executescript(
        """
        PRAGMA foreign_keys=ON;
        CREATE TABLE IF NOT EXISTS machine_semantic_contract (
          contract_id TEXT PRIMARY KEY,
          asset_version_id TEXT NOT NULL UNIQUE REFERENCES knowledge_asset_version(asset_version_id),
          understanding_schema_json TEXT NOT NULL,
          decision_schema_json TEXT NOT NULL,
          calculation_schema_json TEXT NOT NULL,
          constraint_schema_json TEXT NOT NULL,
          action_schema_json TEXT NOT NULL,
          deterministic INTEGER NOT NULL CHECK (deterministic IN (0,1)),
          requires_approval INTEGER NOT NULL CHECK (requires_approval IN (0,1)),
          status TEXT NOT NULL CHECK (status IN ('draft','ready','blocked','enabled')),
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS machine_decision_spec (
          decision_id TEXT PRIMARY KEY,
          asset_version_id TEXT NOT NULL REFERENCES knowledge_asset_version(asset_version_id),
          decision_key TEXT NOT NULL,
          input_schema_json TEXT NOT NULL,
          output_values_json TEXT NOT NULL,
          decision_expression_json TEXT NOT NULL,
          confidence_required REAL,
          status TEXT NOT NULL CHECK (status IN ('draft','ready','blocked','enabled')),
          created_at TEXT NOT NULL,
          UNIQUE(asset_version_id,decision_key)
        );
        CREATE TABLE IF NOT EXISTS machine_calculation_spec (
          calculation_id TEXT PRIMARY KEY,
          asset_version_id TEXT NOT NULL REFERENCES knowledge_asset_version(asset_version_id),
          calculation_key TEXT NOT NULL,
          operation TEXT NOT NULL,
          input_fields_json TEXT NOT NULL,
          output_field TEXT NOT NULL,
          parameters_json TEXT NOT NULL,
          deterministic INTEGER NOT NULL CHECK (deterministic IN (0,1)),
          status TEXT NOT NULL CHECK (status IN ('draft','ready','blocked','enabled')),
          created_at TEXT NOT NULL,
          UNIQUE(asset_version_id,calculation_key)
        );
        CREATE TABLE IF NOT EXISTS machine_constraint_spec (
          constraint_id TEXT PRIMARY KEY,
          asset_version_id TEXT NOT NULL REFERENCES knowledge_asset_version(asset_version_id),
          constraint_key TEXT NOT NULL,
          constraint_type TEXT NOT NULL CHECK (constraint_type IN ('identity','evidence','semantic','consistency','replay','approval','source_write')),
          expression_json TEXT NOT NULL,
          severity TEXT NOT NULL CHECK (severity IN ('low','medium','high','critical')),
          on_fail TEXT NOT NULL CHECK (on_fail IN ('continue','needs_review','block')),
          validator_version TEXT NOT NULL,
          status TEXT NOT NULL CHECK (status IN ('draft','ready','blocked','enabled')),
          created_at TEXT NOT NULL,
          UNIQUE(asset_version_id,constraint_key)
        );
        CREATE TABLE IF NOT EXISTS machine_action_spec (
          action_id TEXT PRIMARY KEY,
          asset_version_id TEXT NOT NULL REFERENCES knowledge_asset_version(asset_version_id),
          action_key TEXT NOT NULL,
          action_type TEXT NOT NULL CHECK (action_type IN ('generate_candidate','apply_terminology','replay_evaluate','route_review','approve','publish')),
          target_type TEXT NOT NULL,
          parameters_json TEXT NOT NULL,
          requires_approval INTEGER NOT NULL CHECK (requires_approval IN (0,1)),
          idempotency_key_template TEXT NOT NULL,
          source_write INTEGER NOT NULL CHECK (source_write=0),
          status TEXT NOT NULL CHECK (status IN ('draft','ready','blocked','enabled')),
          created_at TEXT NOT NULL,
          UNIQUE(asset_version_id,action_key)
        );
        CREATE TABLE IF NOT EXISTS machine_semantics_run (
          run_id TEXT PRIMARY KEY,
          contract_count INTEGER NOT NULL,
          decision_count INTEGER NOT NULL,
          calculation_count INTEGER NOT NULL,
          constraint_count INTEGER NOT NULL,
          action_count INTEGER NOT NULL,
          source_write INTEGER NOT NULL CHECK (source_write=0),
          formal_publication INTEGER NOT NULL CHECK (formal_publication=0),
          created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_machine_contract_status ON machine_semantic_contract(status);
        CREATE INDEX IF NOT EXISTS ix_machine_constraint_gate ON machine_constraint_spec(on_fail,severity,status);
        CREATE INDEX IF NOT EXISTS ix_machine_action_approval ON machine_action_spec(requires_approval,status);
        """
    )


def build(target_path: pathlib.Path) -> dict[str, object]:
    db = sqlite3.connect(str(target_path), timeout=30)
    db.row_factory = sqlite3.Row
    init_specs(db)
    created = now()
    versions = db.execute(
        """
        SELECT v.*,a.asset_key,a.asset_type,a.status AS asset_status,a.title
        FROM knowledge_asset_version v JOIN knowledge_asset a ON a.asset_id=v.asset_id
        ORDER BY v.asset_id,v.version
        """
    ).fetchall()
    for row in versions:
        definition = json_obj(row["definition_json"])
        version_id = row["asset_version_id"]
        asset_status = row["asset_status"]
        spec_status = "enabled" if asset_status == "enabled" else ("blocked" if asset_status in {"blocked", "retired"} else "ready")
        object_type = str(definition.get("object_type") or definition.get("target_object_type") or ("device" if row["asset_type"] == "evaluation_case" else "business_object"))
        understanding = {
            "object_type": object_type,
            "identity_fields": ["source_system", "source_object_id", "canonical_id"],
            "context_fields": ["location_id", "parent_id", "classification_id", "identity_evidence"],
            "raw_fields_preserved": True,
            "source_read_only": True,
        }
        if object_type == "device":
            understanding["identity_fields"] = ["source_schema", "site_id", "asset_number"]
            understanding["context_fields"] = ["location_code", "location_description", "location_parent", "classification_description", "kks"]
        if row["asset_type"] == "evaluation_case":
            understanding["case_input_fields"] = ["input_description", "context_json", "expected_decision"]
        decision_values = ["accepted", "rejected", "needs_review", "blocked"]
        if row["asset_type"] in {"rule", "terminology"}:
            decision_values = ["propose_rule", "keep_original", "needs_review", "blocked"]
        if row["asset_type"] == "evaluation_case" and definition.get("expected_decision"):
            decision_values = [str(definition["expected_decision"]), "needs_review", "blocked"]
        decision_expression = {
            "rule": "evaluate validators and evidence before selecting decision",
            "hard_fail": "blocked",
            "ambiguous": "needs_review",
            "expected_values": decision_values,
        }
        calculation = {
            "operation": definition.get("operation") or definition.get("cleaning_type") or definition.get("rule_type") or "compare_expected",
            "parameters": definition.get("parameters") or {
                "source_term": definition.get("source_term"),
                "target_term": definition.get("target_term"),
            },
        }
        constraints = [
            ("identity_unique", "identity", {"key": ["source_schema", "site_id", "asset_number"], "unique": True}, "critical", "block"),
            ("evidence_required", "evidence", {"required": ["source_snapshot_id", "source_row_hash", "evidence_json"]}, "high", "needs_review"),
            ("semantic_device_output", "semantic", {"output_must_describe": "device", "code_only_forbidden": True}, "high", "needs_review"),
            ("context_consistency", "consistency", {"check": ["location", "classification", "parent_child", "kks"]}, "high", "needs_review"),
            ("replay_before_enable", "replay", {"required": True, "fail_count": 0}, "high", "block"),
            ("approval_before_action", "approval", {"required_for": ["publish", "rewrite"], "separate_from_replay": True}, "high", "block"),
            ("source_write_forbidden", "source_write", {"source_write": False, "formal_publication": False}, "critical", "block"),
        ]
        action_type = "replay_evaluate" if row["asset_type"] == "evaluation_case" else ("apply_terminology" if row["asset_type"] == "terminology" else "generate_candidate")
        action_target = "evaluation_result" if action_type == "replay_evaluate" else "semantic_candidate"
        action_parameters = {
            "asset_key": row["asset_key"],
            "operation": calculation["operation"],
            "parameters": calculation["parameters"],
        }
        contract_status = spec_status
        db.execute(
            """
            INSERT INTO machine_semantic_contract(contract_id,asset_version_id,understanding_schema_json,decision_schema_json,
              calculation_schema_json,constraint_schema_json,action_schema_json,deterministic,requires_approval,status,created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(asset_version_id) DO UPDATE SET understanding_schema_json=excluded.understanding_schema_json,
              decision_schema_json=excluded.decision_schema_json,calculation_schema_json=excluded.calculation_schema_json,
              constraint_schema_json=excluded.constraint_schema_json,action_schema_json=excluded.action_schema_json,
              deterministic=excluded.deterministic,requires_approval=excluded.requires_approval,status=excluded.status
            """,
            (sid("MSC", version_id), version_id, json.dumps(understanding, ensure_ascii=False, sort_keys=True),
             json.dumps({"decision_key": "semantic_decision", "values": decision_values, "expression": decision_expression}, ensure_ascii=False, sort_keys=True),
             json.dumps(calculation, ensure_ascii=False, sort_keys=True), json.dumps({"constraints": [x[0] for x in constraints]}, ensure_ascii=False),
             json.dumps({"action_type": action_type, "target_type": action_target, "requires_approval": action_type != "replay_evaluate"}, ensure_ascii=False),
             1, 1 if action_type != "replay_evaluate" else 0, contract_status, created),
        )
        db.execute(
            """
            INSERT INTO machine_decision_spec(decision_id,asset_version_id,decision_key,input_schema_json,output_values_json,
              decision_expression_json,confidence_required,status,created_at)
            VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT(asset_version_id,decision_key) DO UPDATE SET input_schema_json=excluded.input_schema_json,
              output_values_json=excluded.output_values_json,decision_expression_json=excluded.decision_expression_json,
              status=excluded.status
            """,
            (sid("MSD", version_id), version_id, "semantic_decision", json.dumps(understanding, ensure_ascii=False),
             json.dumps(decision_values, ensure_ascii=False), json.dumps(decision_expression, ensure_ascii=False), 0.95,
             spec_status, created),
        )
        db.execute(
            """
            INSERT INTO machine_calculation_spec(calculation_id,asset_version_id,calculation_key,operation,input_fields_json,
              output_field,parameters_json,deterministic,status,created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(asset_version_id,calculation_key) DO UPDATE SET operation=excluded.operation,
              input_fields_json=excluded.input_fields_json,output_field=excluded.output_field,parameters_json=excluded.parameters_json,
              status=excluded.status
            """,
            (sid("MSCALC", version_id), version_id, "candidate_description", str(calculation["operation"]),
             json.dumps(["original_description", "context"], ensure_ascii=False), "candidate_description",
             json.dumps(calculation["parameters"], ensure_ascii=False), 1, spec_status, created),
        )
        for key, constraint_type, expression, severity, on_fail in constraints:
            db.execute(
                """
                INSERT INTO machine_constraint_spec(constraint_id,asset_version_id,constraint_key,constraint_type,expression_json,
                  severity,on_fail,validator_version,status,created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(asset_version_id,constraint_key) DO UPDATE SET expression_json=excluded.expression_json,
                  severity=excluded.severity,on_fail=excluded.on_fail,status=excluded.status
                """,
                (sid("MSCST", version_id, key), version_id, key, constraint_type, json.dumps(expression, ensure_ascii=False),
                 severity, on_fail, "machine-semantics-contract-v1", spec_status, created),
            )
        db.execute(
            """
            INSERT INTO machine_action_spec(action_id,asset_version_id,action_key,action_type,target_type,parameters_json,
              requires_approval,idempotency_key_template,source_write,status,created_at)
            VALUES (?,?,?,?,?,?,?,?,0,?,?)
            ON CONFLICT(asset_version_id,action_key) DO UPDATE SET action_type=excluded.action_type,
              target_type=excluded.target_type,parameters_json=excluded.parameters_json,
              requires_approval=excluded.requires_approval,status=excluded.status
            """,
            (sid("MSA", version_id), version_id, "primary_action", action_type, action_target,
             json.dumps(action_parameters, ensure_ascii=False), 1 if action_type != "replay_evaluate" else 0,
             "{source_snapshot_id}:{source_schema}:{site_id}:{asset_number}:{asset_version_id}", spec_status, created),
        )
    counts = [
        int(db.execute("SELECT count(*) FROM machine_semantic_contract").fetchone()[0]),
        int(db.execute("SELECT count(*) FROM machine_decision_spec").fetchone()[0]),
        int(db.execute("SELECT count(*) FROM machine_calculation_spec").fetchone()[0]),
        int(db.execute("SELECT count(*) FROM machine_constraint_spec").fetchone()[0]),
        int(db.execute("SELECT count(*) FROM machine_action_spec").fetchone()[0]),
    ]
    run_id = sid("MSR", created)
    db.execute(
        """INSERT INTO machine_semantics_run(run_id,contract_count,decision_count,calculation_count,constraint_count,
          action_count,source_write,formal_publication,created_at) VALUES (?,?,?,?,?,?,0,0,?)""",
        (run_id, *counts, created),
    )
    db.commit()
    db.close()
    return {"run_id": run_id, "contract_count": counts[0], "decision_count": counts[1], "calculation_count": counts[2], "constraint_count": counts[3], "action_count": counts[4], "source_write": False, "formal_publication": False}


def main() -> None:
    parser = argparse.ArgumentParser(description="Build machine-readable semantic execution contracts")
    parser.add_argument("--target-db", type=pathlib.Path, default=DEFAULT_TARGET)
    args = parser.parse_args()
    print(json.dumps(build(args.target_db.resolve()), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
