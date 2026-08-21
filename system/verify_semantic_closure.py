"""Verify that the local semantic pipeline is closed with controlled gaps.

The verifier deliberately accepts explicit ``needs_evidence`` and ``isolated``
gaps.  It fails only when a stage is missing, unsafe flags appear, or a gap is
unclassified.  This distinguishes a governed system from a falsely green
report that silently drops missing source data.
"""
from __future__ import annotations

import json
import pathlib

from pipeline.contracts import connect_readonly

ROOT = pathlib.Path(__file__).resolve().parent
TARGET = ROOT / "data" / "unified_semantics.sqlite3"
CANONICAL_TARGET = ROOT / "data" / "canonical_semantic.sqlite3"
OUTPUT = ROOT / "data" / "semantic_closure_verification.json"


def verify(target: pathlib.Path = TARGET) -> dict[str, object]:
    db = connect_readonly(target)
    failures: list[str] = []
    latest_closure = db.execute("SELECT * FROM semantic_closure_run ORDER BY created_at DESC LIMIT 1").fetchone()
    latest_coverage = db.execute("SELECT * FROM semantic_coverage_run ORDER BY created_at DESC LIMIT 1").fetchone()
    latest_runtime = db.execute("SELECT * FROM semantic_runtime_contract_run ORDER BY created_at DESC LIMIT 1").fetchone()
    latest_governance = db.execute("SELECT * FROM semantic_governance_quality_run ORDER BY created_at DESC LIMIT 1").fetchone() if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='semantic_governance_quality_run'").fetchone() else None
    latest_replay = db.execute("SELECT * FROM semantic_state_transition_run ORDER BY created_at DESC LIMIT 1").fetchone()
    latest_fact = db.execute("SELECT * FROM semantic_fact_builder_run ORDER BY created_at DESC LIMIT 1").fetchone()
    latest_decision = db.execute("SELECT * FROM semantic_decision_layer_run ORDER BY created_at DESC LIMIT 1").fetchone()
    adapter_count = int(db.execute("SELECT count(*) FROM semantic_execution_adapter WHERE status='active'").fetchone()[0])
    external_adapter_count = int(db.execute("SELECT count(*) FROM semantic_execution_adapter WHERE mode='external_enabled'").fetchone()[0])
    unsafe_ledger = int(db.execute("SELECT count(*) FROM semantic_execution_ledger WHERE source_write<>0 OR formal_publication<>0").fetchone()[0])
    gap_statuses = [str(row[0]) for row in db.execute("SELECT DISTINCT status FROM semantic_coverage_gap WHERE run_id=(SELECT run_id FROM semantic_coverage_run ORDER BY created_at DESC LIMIT 1)").fetchall()]
    gap_count = int(db.execute("SELECT count(*) FROM semantic_coverage_gap WHERE run_id=(SELECT run_id FROM semantic_coverage_run ORDER BY created_at DESC LIMIT 1)").fetchone()[0])
    controlled_review_count = 0
    unexpected_review_count = 0
    if latest_replay is not None:
        machine = db.execute(
            "SELECT manual_only_states_json FROM ontology_state_machine WHERE machine_id='SM:DEFECT:v1'"
        ).fetchone()
        try:
            manual_only = set(json.loads(machine[0] or "[]")) if machine else set()
        except (TypeError, json.JSONDecodeError):
            manual_only = set()
        review_rows = db.execute(
            "SELECT evidence_json FROM semantic_state_transition WHERE replay_run_id=? AND status='needs_review'",
            (latest_replay["run_id"],),
        ).fetchall()
        for row in review_rows:
            try:
                evidence = json.loads(row[0] or "{}")
            except (TypeError, json.JSONDecodeError):
                evidence = {}
            if str(evidence.get("canonical_state") or "").upper() in manual_only:
                controlled_review_count += 1
            else:
                unexpected_review_count += 1
    db.close()

    canonical_run = None
    canonical_inference = None
    if CANONICAL_TARGET.exists():
        canonical = connect_readonly(CANONICAL_TARGET)
        canonical_run = canonical.execute("SELECT * FROM canonical_projection_run ORDER BY created_at DESC LIMIT 1").fetchone()
        if canonical.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='canonical_inference_run'").fetchone():
            canonical_inference = canonical.execute("SELECT * FROM canonical_inference_run ORDER BY created_at DESC LIMIT 1").fetchone()
        canonical.close()
    else:
        failures.append("CANONICAL_MODEL_MISSING")

    if latest_closure is None or latest_closure["status"] not in {"completed", "completed_with_gaps"}:
        failures.append("CLOSURE_RUN_NOT_COMPLETED")
    if latest_coverage is None or latest_coverage["status"] not in {"completed", "partial"}:
        failures.append("COVERAGE_RUN_MISSING")
    if latest_runtime is None or latest_runtime["status"] not in {"completed", "completed_with_isolation"}:
        failures.append("RUNTIME_CONTRACT_NOT_COMPLETED")
    if latest_runtime and int(latest_runtime["traced_event_count"] or 0) != int(latest_runtime["event_count"] or 0):
        failures.append("EVENT_TRACE_COVERAGE_INCOMPLETE")
    if latest_runtime and int(latest_runtime["authority_count"] or 0) < 5:
        failures.append("AUTHORITY_POLICY_INCOMPLETE")
    if latest_governance is None or latest_governance["status"] not in {"completed", "completed_with_isolation"}:
        failures.append("GOVERNANCE_CONTRACT_NOT_COMPLETED")
    if latest_governance and int(latest_governance["critical_count"] or 0) > 0:
        failures.append("GOVERNANCE_HAS_CRITICAL_ISSUES")
    if latest_governance and (int(latest_governance["source_write"] or 0) != 0 or int(latest_governance["formal_publication"] or 0) != 0):
        failures.append("GOVERNANCE_UNSAFE_WRITE_FLAGS")
    if latest_replay is None or latest_replay["replay_version"] != "SBRV:state-transition.defect-canonical-v2":
        failures.append("STATE_REPLAY_NOT_COMPLETED_V2")
    if latest_replay and latest_replay["status"] != "completed" and unexpected_review_count > 0:
        failures.append("STATE_REPLAY_NOT_COMPLETED_V2")
    if unexpected_review_count > 0:
        failures.append("STATE_REPLAY_HAS_REVIEW_TRANSITIONS")
    if latest_fact is None or latest_fact["status"] not in {"completed", "needs_review"}:
        failures.append("FACT_BUILDER_NOT_COMPLETED")
    if latest_decision is None or latest_decision["status"] not in {"completed", "needs_review"}:
        failures.append("DECISION_LAYER_NOT_COMPLETED")
    if canonical_run is None or canonical_run["status"] != "completed":
        failures.append("CANONICAL_MODEL_NOT_COMPLETED")
    if canonical_run and int(canonical_run["validation_error_count"] or 0) != 0:
        failures.append("CANONICAL_MODEL_VALIDATION_ERRORS")
    if canonical_run and (int(canonical_run["source_write"] or 0) != 0 or int(canonical_run["formal_publication"] or 0) != 0):
        failures.append("CANONICAL_MODEL_UNSAFE_FLAGS")
    if canonical_inference is None or canonical_inference["status"] != "completed":
        failures.append("OWL_RL_REPLAY_NOT_COMPLETED")
    if canonical_inference and (int(canonical_inference["source_write"] or 0) != 0 or int(canonical_inference["formal_publication"] or 0) != 0):
        failures.append("OWL_RL_REPLAY_UNSAFE_FLAGS")
    if adapter_count < 1:
        failures.append("EXECUTION_ADAPTER_MISSING")
    if external_adapter_count:
        failures.append("EXTERNAL_ADAPTER_ENABLED")
    if unsafe_ledger:
        failures.append("UNSAFE_EXECUTION_LEDGER")
    if any(status not in {"isolated", "needs_evidence", "resolved"} for status in gap_statuses):
        failures.append("UNCLASSIFIED_COVERAGE_GAP")

    result = {
        "status": "FAIL" if failures else ("PASS_WITH_REVIEW" if controlled_review_count else "PASS"),
        "closureStatus": latest_closure["status"] if latest_closure else None,
        "coverageStatus": latest_coverage["status"] if latest_coverage else None,
        "coverageGapCount": gap_count,
        "coverageGapStatuses": gap_statuses,
        "runtimeContract": dict(latest_runtime) if latest_runtime else None,
        "governanceContract": dict(latest_governance) if latest_governance else None,
        "stateReplay": dict(latest_replay) if latest_replay else None,
        "controlledReviewTransitionCount": controlled_review_count,
        "unexpectedReviewTransitionCount": unexpected_review_count,
        "factBuilder": dict(latest_fact) if latest_fact else None,
        "decisionLayer": dict(latest_decision) if latest_decision else None,
        "canonicalModel": dict(canonical_run) if canonical_run else None,
        "owlRlReplay": dict(canonical_inference) if canonical_inference else None,
        "executionAdapterCount": adapter_count,
        "externalAdapterCount": external_adapter_count,
        "unsafeLedgerCount": unsafe_ledger,
        "failures": failures,
        "sourceWrite": False,
        "formalPublication": False,
    }
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


if __name__ == "__main__":
    result = verify()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result["status"] == "FAIL":
        raise SystemExit(1)
