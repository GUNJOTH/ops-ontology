"""Canonical RDF pipeline orchestration.

The builder owns domain-specific projection code, while this module owns the
ordered, replayable stages and their run evidence.  Keeping the stage graph in
one place prevents the canonical build from becoming another opaque script.

The OWL 2 RL stage is intentionally deferred until the RDF/provenance
artifacts are persisted: the existing bounded reasoner consumes the materialized
Canonical Dataset.  It is still one pipeline stage and its result is recorded
in the final manifest and local run-asset index.
"""
from __future__ import annotations

import importlib
import json
import time
from pathlib import Path
from typing import Any, Callable

from pipeline.contracts import content_hash, utc_now, write_json_atomic
from pipeline.run_assets import register_run

CANONICAL_STAGE_ORDER = (
    "snapshot",
    "mapping",
    "rdf_graph",
    "shacl",
    "owl_rl",
    "provenance",
    "manifest",
    "release",
)


def _stage_result(stage_id: str, started_at: str, started_clock: float, **outputs: Any) -> dict[str, Any]:
    return {
        "stageId": stage_id,
        "status": "completed",
        "startedAt": started_at,
        "finishedAt": utc_now(),
        "durationSeconds": round(time.perf_counter() - started_clock, 6),
        "outputs": outputs,
        "sourceWrite": False,
        "formalPublication": False,
    }


def _run_stage(stage_id: str, action: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    started_at = utc_now()
    started_clock = time.perf_counter()
    outputs = action()
    return _stage_result(stage_id, started_at, started_clock, **outputs)


def _identity_preflight_issue(builder: Any) -> list[dict[str, str]]:
    if builder.identity_preflight.get("enabled") and builder.identity_preflight.get("status") != "ready":
        return [{
            "focusNode": "identity://unified_device",
            "path": "identity://preflight",
            "code": "identityPreflight",
            "severity": "Violation",
            "message": "全量统一设备身份投影预检未通过，未写入来源身份图",
        }]
    return []


def run_canonical_pipeline(
    builder: Any,
    source_db: Any,
    identity_db: Any,
    target_db: Any,
    *,
    root: Path,
) -> dict[str, Any]:
    """Run the standard Canonical RDF stages against already-open databases."""
    stages: list[dict[str, Any]] = []
    issues: list[dict[str, str]] = []

    def snapshot() -> dict[str, Any]:
        snapshot = source_db.execute(
            "SELECT source_snapshot_id,count(*) AS row_count "
            "FROM semantic_object_instance WHERE source_snapshot_id IS NOT NULL "
            "GROUP BY source_snapshot_id ORDER BY row_count DESC,source_snapshot_id LIMIT 1"
        ).fetchone()
        builder.source_snapshot_id = str(snapshot["source_snapshot_id"]) if snapshot else None
        builder.prepare_identity_projection(identity_db)
        builder.ensure_target_schema(target_db)
        return {
            "sourceSnapshotId": builder.source_snapshot_id,
            "identityPreflight": builder.identity_preflight,
        }

    def mapping() -> dict[str, Any]:
        builder.build_ontology(source_db)
        builder.build_objects(source_db)
        builder.build_identity_assertions(source_db)
        return {
            "vocabulary": builder.vocabulary_counts,
            "identityAssertions": sum(1 for _ in builder.dataset.quads((None, None, None, None))),
        }

    def rdf_graph() -> dict[str, Any]:
        builder.build_relations(source_db)
        builder.build_events(source_db)
        builder.build_facts_states_rules(source_db)
        return {
            "graphCount": len(list(builder.dataset.contexts())),
            "statementCount": len(builder.dataset),
        }

    def shacl() -> dict[str, Any]:
        nonlocal issues
        issues = builder.validate_dataset(builder.dataset)
        issues.extend(_identity_preflight_issue(builder))
        return {"validationErrorCount": len(issues), "conforms": not issues}

    # The reasoner consumes the persisted RDF artifacts.  Register the stage
    # before provenance so the final manifest preserves the declared pipeline
    # order; its result is filled after persistence below.
    stages.append({
        "stageId": "owl_rl",
        "status": "deferred_until_rdf_persisted",
        "sourceWrite": False,
        "formalPublication": False,
    })

    stages.insert(0, _run_stage("snapshot", snapshot))
    stages.insert(1, _run_stage("mapping", mapping))
    stages.insert(2, _run_stage("rdf_graph", rdf_graph))
    stages.insert(3, _run_stage("shacl", shacl))

    provenance_started = utc_now()
    provenance_clock = time.perf_counter()
    manifest = builder.serialize_and_persist(target_db, issues, identity_db)
    stages.insert(5, _stage_result(
        "provenance",
        provenance_started,
        provenance_clock,
        coveredStatements=manifest.get("provenanceCoverage", {}).get("coveredStatements", 0),
        eligibleStatements=manifest.get("provenanceCoverage", {}).get("eligibleStatements", 0),
    ))

    owl_stage = stages[4]
    owl_started = utc_now()
    owl_clock = time.perf_counter()
    reasoner = importlib.import_module("replay_owl_rl")
    inference_manifest = reasoner.persist_inference(builder.target)
    owl_stage.update(_stage_result(
        "owl_rl",
        owl_started,
        owl_clock,
        runId=inference_manifest.get("runId"),
        ruleSetVersion=inference_manifest.get("ruleSetVersion"),
        inputStatementCount=inference_manifest.get("inputStatementCount", 0),
        inferredStatementCount=inference_manifest.get("inferredStatementCount", 0),
        iterationCount=inference_manifest.get("iterationCount", 0),
    ))

    manifest["pipeline"] = {
        "stageOrder": list(CANONICAL_STAGE_ORDER),
        "stages": stages,
        "owlRl": inference_manifest,
    }
    manifest["artifacts"]["owlRlTrig"] = inference_manifest.get("artifacts", {}).get("trig")
    manifest["artifacts"]["owlRlJsonLd"] = inference_manifest.get("artifacts", {}).get("jsonld")

    run_dir = root / "canonical-runs" / builder.run_id
    manifest_file = run_dir / "projection_manifest.json"
    write_json_atomic(manifest_file, manifest)
    target_db.execute(
        "UPDATE canonical_projection_run SET manifest_json=? WHERE run_id=?",
        (json.dumps(manifest, ensure_ascii=False), builder.run_id),
    )
    target_db.commit()

    manifest_started = utc_now()
    manifest_clock = time.perf_counter()
    stages.insert(6, _stage_result(
        "manifest",
        manifest_started,
        manifest_clock,
        path=str(manifest_file),
        contentHash=content_hash(manifest),
    ))

    release_started = utc_now()
    release_clock = time.perf_counter()
    release = register_run(
        root,
        run_id=builder.run_id,
        asset_type="canonical-rdf-dataset",
        manifest_path=manifest_file,
        idempotency_key=f"canonical-rdf-build:{builder.ontology_version}:{builder.run_id}",
        inputs={"sourceDb": str(builder.source), "identitySource": str(builder.identity_source) if builder.identity_source else None},
        outputs={
            "sourceWrite": False,
            "formalPublication": False,
            "validationErrorCount": manifest.get("validationErrorCount", 0),
            "owlRlRunId": inference_manifest.get("runId"),
        },
        source_snapshot_id=builder.source_snapshot_id,
        replayable=True,
    )
    stages.insert(7, _stage_result("release", release_started, release_clock, asset=release))
    manifest["release"] = release
    manifest["pipeline"]["stages"] = stages
    write_json_atomic(manifest_file, manifest)
    target_db.execute(
        "UPDATE canonical_projection_run SET manifest_json=? WHERE run_id=?",
        (json.dumps(manifest, ensure_ascii=False), builder.run_id),
    )
    target_db.commit()
    return manifest
