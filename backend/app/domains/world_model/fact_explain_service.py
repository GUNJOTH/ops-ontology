"""World-model fact and decision explanation services."""
from __future__ import annotations

from typing import Any

from fastapi import HTTPException

from app.core.db import unified_semantics_connection
from app.core.utils import ontology_trace_id, parse_json_array
from app.domains.ontology.service import ontology_meta_tables


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
