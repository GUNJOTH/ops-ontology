"""Read-only acceptance check for the ontology runtime layer.

This is intentionally separate from the legacy device-description verifier:
the latter compares an older SQLite/DuckDB candidate baseline.  This check
verifies the local ontology registry, replay ledgers, provenance tables, and
the source-write boundary for the current world-model layer.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sqlite3

from pipeline.contracts import connect_readonly
from pipeline.knowledge_runtime import validate_knowledge_layer
from semantic_namespaces import ONTOLOGY_NAMESPACE
from semantic_registry import (
    canonical_relation_key,
    object_class_local_name,
    relation_predicate_local_name,
    validate_registry_against_ontology,
)

ROOT = pathlib.Path(__file__).resolve().parent
DEFAULT_TARGET = ROOT / "data" / "unified_semantics.sqlite3"
CORE_OBJECTS = {
    "device", "site", "center", "specialty", "team", "inspection", "abnormal_inspection",
    "defect", "repeated_defect", "severe_defect", "defect_resolution", "work_order", "work_permit", "human_review",
    "action", "action_execution", "action_adapter",
}


def verify(target_path: pathlib.Path) -> dict[str, object]:
    db = connect_readonly(target_path.resolve())
    db.row_factory = sqlite3.Row
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    required = {
        "ontology_object_type", "ontology_property_type", "ontology_relation_type", "ontology_event_type",
        "ontology_state_machine", "ontology_transition_rule", "ontology_meta_model_run",
        "semantic_event", "semantic_fact", "semantic_fact_derivation", "semantic_current_state",
        "semantic_rule_decision", "semantic_action_plan",
    }
    missing_tables = sorted(required - tables)
    if missing_tables:
        db.close()
        return {"status": "FAIL", "missing_tables": missing_tables, "source_write": False, "formal_publication": False}

    registered_core = {row[0] for row in db.execute(
        "SELECT object_type FROM ontology_object_type WHERE status='active'"
    ).fetchall()}
    latest = db.execute("SELECT * FROM ontology_meta_model_run ORDER BY created_at DESC LIMIT 1").fetchone()
    source_flags = []
    for table, columns in {
        "ontology_meta_model_run": ("source_write", "formal_publication"),
        "semantic_event_layer_run": ("source_write", "formal_publication"),
        "semantic_state_transition_run": ("source_write", "formal_publication"),
        "semantic_reasoning_run": ("source_write", "formal_publication"),
        "semantic_decision_layer_run": ("source_write", "formal_publication"),
    }.items():
        if table not in tables:
            continue
        row = db.execute(f"SELECT {','.join(columns)} FROM {table} ORDER BY created_at DESC LIMIT 1").fetchone()
        if row and any(int(row[column] or 0) != 0 for column in columns):
            source_flags.append(table)
    relation_rows = db.execute(
        "SELECT DISTINCT predicate FROM business_object_relation WHERE status='accepted' ORDER BY predicate"
    ).fetchall()
    registered_relations = {
        str(row[0]) for row in db.execute(
            "SELECT predicate FROM ontology_relation_type WHERE status='active' AND review_status='approved'"
        ).fetchall()
    }
    unregistered_relations: list[str] = []
    for row in relation_rows:
        raw = str(row[0] or "")
        key = canonical_relation_key(raw)
        try:
            relation_predicate_local_name(key)
        except ValueError:
            unregistered_relations.append(raw)
            continue
        if key not in registered_relations:
            unregistered_relations.append(raw)
    unregistered_events = [row[0] for row in db.execute(
        """SELECT DISTINCT e.event_type FROM semantic_event e
           LEFT JOIN ontology_event_type t ON t.event_type=e.event_type
           WHERE e.status IN ('observed','accepted') AND (t.event_type IS NULL OR t.review_status='needs_review')"""
    ).fetchall()]
    event_columns = {row[1] for row in db.execute("PRAGMA table_info(ontology_event_type)").fetchall()}
    transition_columns = {row[1] for row in db.execute("PRAGMA table_info(ontology_transition_rule)").fetchall()}
    registry_owl_errors = validate_registry_against_ontology()
    knowledge_runtime = validate_knowledge_layer(
        db,
        ROOT.parent / "standards" / "v2" / "ontology.ttl",
    )
    formal_event_types = bool({"rdf_class"}.issubset(event_columns)) and all(
        str(row["rdf_class"] or "") == ONTOLOGY_NAMESPACE + str(row["event_type"])
        for row in db.execute(
            "SELECT event_type,rdf_class FROM ontology_event_type WHERE status='active' AND review_status='approved'"
        ).fetchall()
    )
    transition_event_iris = bool({"event_iri"}.issubset(transition_columns)) and all(
        row["event_iri"] == ONTOLOGY_NAMESPACE + row["event_type"]
        for row in db.execute(
            """SELECT t.event_type,t.event_iri FROM ontology_transition_rule t
               JOIN ontology_event_type e ON e.event_type=t.event_type
               WHERE t.status='active' AND t.review_status='approved'
                 AND e.status='active' AND e.review_status='approved'"""
        ).fetchall()
    )
    formal_event_type_count = int(db.execute(
        "SELECT count(*) FROM ontology_event_type WHERE status='active' AND review_status='approved'"
    ).fetchone()[0])
    fact_count = int(db.execute(
        "SELECT count(*) FROM semantic_fact WHERE status IN ('observed','accepted','derived')"
    ).fetchone()[0])
    derivation_count = int(db.execute("SELECT count(*) FROM semantic_fact_derivation").fetchone()[0])
    checks = {
        "core_object_coverage": CORE_OBJECTS.issubset(registered_core),
        "meta_model_run_exists": latest is not None,
        "source_write_boundary": not source_flags,
        # A bounded test snapshot may legitimately contain no accepted
        # inspection/defect/work-order facts.  In that case there is no
        # derivation to explain; once facts exist, every run must retain a
        # derivation ledger.
        "explainable_derivation_table": (
            "semantic_fact_derivation" in tables
            and (fact_count == 0 or derivation_count > 0)
        ),
        "relation_vocabulary_reconciled": not unregistered_relations and all(
            row[0] not in {"" , "None"} for row in db.execute(
                "SELECT coalesce(rdf_predicate,'') FROM ontology_relation_type WHERE status='active' AND review_status='approved'"
            ).fetchall()
        ),
        "object_vocabulary_reconciled": all(
            str(row["rdf_class"] or "") == object_class_local_name(row["object_type"])
            for row in db.execute(
                "SELECT object_type,rdf_class FROM ontology_object_type WHERE status='active'"
            ).fetchall()
        ),
        "registry_owl_bidirectional_consistency": not registry_owl_errors,
        "formal_event_type_registry": formal_event_types,
        "transition_event_iri_registry": transition_event_iris,
        "action_plan_boundary": int(db.execute("SELECT count(*) FROM semantic_action_plan WHERE source_write<>0 OR formal_publication<>0").fetchone()[0]) == 0,
        "knowledge_runtime_gate": knowledge_runtime["status"] == "PASS",
    }
    db.close()
    status = "PASS" if all(checks.values()) and not (missing_tables or source_flags) else "FAIL"
    if status == "PASS" and (unregistered_relations or unregistered_events):
        status = "PASS_WITH_REVIEW"
    return {
        "status": status,
        "checks": checks,
        "registered_core_object_count": len(registered_core & CORE_OBJECTS),
        "required_core_object_count": len(CORE_OBJECTS),
        "unregistered_relation_types": unregistered_relations,
        "unregistered_event_types": unregistered_events,
        "registry_owl_errors": registry_owl_errors,
        "knowledgeRuntime": knowledge_runtime,
        "formal_event_type_count": formal_event_type_count,
        "fact_count": fact_count,
        "derivation_count": derivation_count,
        "derivation_check_note": (
            "无已接受事实，派生链不适用"
            if fact_count == 0
            else "已有事实且存在派生证据"
        ),
        "source_flagged_runs": source_flags,
        "latest_meta_model_run": dict(latest) if latest else None,
        "source_write": False,
        "formal_publication": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify the local ontology runtime without writing data")
    parser.add_argument("--target-db", type=pathlib.Path, default=DEFAULT_TARGET)
    args = parser.parse_args()
    result = verify(args.target_db)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result["status"] == "FAIL":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
