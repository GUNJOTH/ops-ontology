"""Run the local semantic closure pipeline in one auditable command.

The command refreshes only the local semantic overlay, in dependency order:
event projection -> governance/runtime -> state replay -> deterministic facts
-> decisions/actions -> execution ledger -> canonical RDF projection ->
coverage/backlog report.  It creates a local backup before any overlay writes
and never enables a source adapter or publishes upstream.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import shutil
import sqlite3
import sys
from typing import Any

from pipeline.contracts import PipelineContext, connect_local
from pipeline.dag import PipelineRunner, load_spec

ROOT = pathlib.Path(__file__).resolve().parent
DEFAULT_TARGET = ROOT / "data" / "unified_semantics.sqlite3"
DEFAULT_WORKFLOW = ROOT / "data" / "semantic_workflow.sqlite3"
DEFAULT_METADATA = ROOT.parent / "pilots" / "metadata" / "results" / "metadata-semantic-v1-20260815T-v2" / "metadata_semantics.sqlite3"
DEFAULT_SPEC = ROOT / "pipelines" / "semantic_closure.json"


def timestamp() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def ensure_schema(db: sqlite3.Connection) -> None:
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS semantic_closure_run (
          run_id TEXT PRIMARY KEY,
          pipeline_version TEXT NOT NULL,
          backup_path TEXT NOT NULL,
          status TEXT NOT NULL CHECK(status IN ('completed','completed_with_gaps','failed')),
          stage_results_json TEXT NOT NULL,
          coverage_run_id TEXT,
          source_write INTEGER NOT NULL CHECK(source_write=0),
          formal_publication INTEGER NOT NULL CHECK(formal_publication=0),
          created_at TEXT NOT NULL,
          finished_at TEXT
        );
        CREATE INDEX IF NOT EXISTS ix_semantic_closure_run_created
          ON semantic_closure_run(created_at);
        """
    )


def run(
    target: pathlib.Path,
    workflow: pathlib.Path,
    metadata: pathlib.Path,
    spec_path: pathlib.Path = DEFAULT_SPEC,
    resume_manifest: pathlib.Path | None = None,
) -> dict[str, object]:
    if not target.exists():
        raise FileNotFoundError(target)
    backup_dir = ROOT / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = backup_dir / f"unified-semantics-pre-closure-{timestamp()}.sqlite3"
    shutil.copy2(target, backup)
    spec = load_spec(spec_path.resolve())
    pipeline_version = str(spec.get("version") or "semantic-closure-v2")
    run_id = f"closure-{timestamp()}"
    pipeline_manifest = ROOT / "reports" / "pipeline-runs" / f"{run_id}.json"
    stages: dict[str, object] = {}
    created = dt.datetime.now(dt.timezone.utc).isoformat()
    db = connect_local(target, timeout=30)
    try:
        ensure_schema(db)
        db.execute(
            "INSERT INTO semantic_closure_run(run_id,pipeline_version,backup_path,status,stage_results_json,source_write,formal_publication,created_at) VALUES (?,?,?,'failed',?,0,0,?)",
            (run_id, pipeline_version, str(backup), json.dumps({"pipelineId": spec.get("pipelineId"), "status": "running"}, ensure_ascii=False), created),
        )
        db.commit()
    finally:
        db.close()

    sys.path.insert(0, str(ROOT))
    from build_business_semantics_layer import build as build_business_semantics
    from build_canonical_semantic_model import CanonicalBuilder
    from build_decision_action_layer import build as build_decisions
    from build_defect_status_dictionary import build as build_defect_status_dictionary
    from build_ontology_meta_model import build as build_ontology_meta_model
    from build_semantic_action_catalog import build as build_action_catalog
    from build_semantic_coverage import build as build_coverage
    from build_semantic_event_layer import build as build_events
    from build_semantic_execution_layer import build as build_execution
    from build_semantic_fact_builders import build as build_facts
    from build_semantic_fact_layer import build as build_fact_layer
    from build_semantic_governance_contract import build as build_governance
    from build_semantic_runtime_contract import build as build_runtime_contract
    from build_unified_semantics_layer import build as build_unified
    from build_unified_semantics_layer import latest_identity_db
    from execute_semantic_reasoning import execute as execute_reasoning
    from execute_state_transitions import execute as replay_states
    from replay_owl_rl import persist_inference

    handlers = {
        "unified_semantics_layer": lambda _context, _dependencies: build_unified(latest_identity_db(), target),
        "business_semantics_layer": lambda _context, _dependencies: build_business_semantics(workflow, latest_identity_db(), target),
        "fact_layer": lambda _context, _dependencies: build_fact_layer(target),
        "event_projection": lambda _context, _dependencies: build_events(target),
        "semantic_reasoning": lambda _context, _dependencies: execute_reasoning(target),
        "defect_status_dictionary": lambda _context, _dependencies: build_defect_status_dictionary(target, latest_identity_db()),
        "ontology_meta_model": lambda _context, _dependencies: build_ontology_meta_model(target),
        "runtime_contract": lambda _context, _dependencies: build_runtime_contract(target),
        "governance_contract": lambda _context, _dependencies: build_governance(target),
        "state_replay": lambda _context, _dependencies: replay_states(target),
        "fact_builders": lambda _context, _dependencies: build_facts(target),
        "action_catalog": lambda _context, _dependencies: build_action_catalog(target),
        "decision_action": lambda _context, _dependencies: build_decisions(target),
        "execution_ledger": lambda _context, _dependencies: build_execution(target),
        "canonical_projection": lambda _context, _dependencies: CanonicalBuilder(
            target,
            ROOT / "data" / "canonical_semantic.sqlite3",
            identity_source=latest_identity_db(),
        ).run(),
        "owl_rl_replay": lambda _context, _dependencies: persist_inference(ROOT / "data" / "canonical_semantic.sqlite3"),
        "coverage": lambda _context, _dependencies: build_coverage(target, workflow, metadata),
    }
    previous_manifest: dict[str, Any] | None = None
    if resume_manifest is not None:
        previous_manifest = json.loads(resume_manifest.resolve().read_text(encoding="utf-8"))
    context = PipelineContext(
        pipeline_id=str(spec.get("pipelineId") or "semantic-closure"),
        pipeline_version=pipeline_version,
        run_id=run_id,
        root=ROOT,
        parameters={
            "targetDb": str(target),
            "workflowDb": str(workflow),
            "metadataDb": str(metadata),
            "sourceWrite": False,
            "formalPublication": False,
        },
        manifest_path=pipeline_manifest,
        resume_manifest=previous_manifest,
    )
    pipeline_run: dict[str, Any] | None = None
    try:
        pipeline_run = PipelineRunner(spec, context).run(handlers)
        stages = dict(pipeline_run["outputs"])
        coverage = stages.get("coverage") or {}
        canonical = stages.get("canonical_projection") or {}
        inference = stages.get("owl_rl_replay") or {}
        final_status = "completed" if coverage.get("status") == "completed" and int(canonical.get("validationErrorCount") or 0) == 0 and inference.get("status") == "completed" else "completed_with_gaps"
        finished = dt.datetime.now(dt.timezone.utc).isoformat()
        db = connect_local(target, timeout=30)
        try:
            db.execute(
                "UPDATE semantic_closure_run SET status=?,stage_results_json=?,coverage_run_id=?,finished_at=? WHERE run_id=?",
                (final_status, json.dumps(pipeline_run, ensure_ascii=False), coverage.get("runId"), finished, run_id),
            )
            db.commit()
        finally:
            db.close()
        return {
            "runId": run_id,
            "status": final_status,
            "backupPath": str(backup),
            "stages": stages,
            "pipelineManifest": str(pipeline_manifest),
            "sourceWrite": False,
            "formalPublication": False,
        }
    except Exception as exc:
        db = connect_local(target, timeout=30)
        try:
            db.execute(
                "UPDATE semantic_closure_run SET status='failed',stage_results_json=?,finished_at=? WHERE run_id=?",
                (json.dumps({"pipeline": pipeline_run, "stages": stages, "error": str(exc)}, ensure_ascii=False), dt.datetime.now(dt.timezone.utc).isoformat(), run_id),
            )
            db.commit()
        finally:
            db.close()
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the local semantic closure pipeline")
    parser.add_argument("--target-db", type=pathlib.Path, default=DEFAULT_TARGET)
    parser.add_argument("--workflow-db", type=pathlib.Path, default=DEFAULT_WORKFLOW)
    parser.add_argument("--metadata-db", type=pathlib.Path, default=DEFAULT_METADATA)
    parser.add_argument("--spec", type=pathlib.Path, default=DEFAULT_SPEC, help="Declarative JSON DAG specification")
    parser.add_argument("--resume-manifest", type=pathlib.Path, help="Resume completed steps from a prior pipeline manifest")
    args = parser.parse_args()
    print(json.dumps(run(args.target_db.resolve(), args.workflow_db.resolve(), args.metadata_db.resolve(), args.spec.resolve(), args.resume_manifest.resolve() if args.resume_manifest else None), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
