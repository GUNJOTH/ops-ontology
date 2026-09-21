"""Entrypoint helpers for single-step high-risk pipelines.

Mature build, replay and publication commands keep their own business
manifests.  This module adds a shared run envelope around them without moving
business data into another store or changing source-system boundaries.
"""
from __future__ import annotations

import argparse
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from .contracts import PipelineContext, idempotency_key
from .dag import PipelineRunner
from .run_assets import register_run

Handler = Callable[[PipelineContext, Mapping[str, Any]], Mapping[str, Any]]


class PipelineStepError(RuntimeError):
    """A failed step that still has a useful business result to print."""

    def __init__(self, message: str, payload: Mapping[str, Any]):
        super().__init__(message)
        self.payload = dict(payload)


def add_pipeline_arguments(parser: argparse.ArgumentParser) -> None:
    """Add common run-manifest and resume options to a command parser."""
    parser.add_argument(
        "--pipeline-manifest",
        type=Path,
        help="Pipeline run manifest; defaults to a unique file under system/reports/pipeline-runs",
    )
    parser.add_argument(
        "--resume-manifest",
        type=Path,
        help="Resume completed steps from a previous PipelineContext run manifest",
    )


def _run_id(pipeline_id: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{pipeline_id}-{stamp}-{uuid.uuid4().hex[:10]}"


def _default_manifest(root: Path, pipeline_id: str, run_id: str) -> Path:
    return root / "reports" / "pipeline-runs" / f"{pipeline_id}-{run_id}.json"


def load_resume_manifest(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    payload = json.loads(path.resolve().read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"pipeline resume manifest must be an object: {path}")
    return payload


def run_single_step(
    *,
    pipeline_id: str,
    pipeline_version: str,
    step_id: str,
    root: Path,
    parameters: Mapping[str, Any],
    handler: Handler,
    manifest_path: Path | None = None,
    resume_manifest_path: Path | None = None,
    allow_formal_publication: bool = False,
) -> dict[str, Any]:
    """Run one existing command inside the shared PipelineContext contract."""
    run_id = _run_id(pipeline_id)
    output_manifest = (manifest_path or _default_manifest(root, pipeline_id, run_id)).resolve()
    context = PipelineContext(
        pipeline_id=pipeline_id,
        pipeline_version=pipeline_version,
        run_id=run_id,
        root=root,
        parameters={
            **dict(parameters),
            "sourceWrite": False,
            "formalPublication": allow_formal_publication,
        },
        manifest_path=output_manifest,
        resume_manifest=load_resume_manifest(resume_manifest_path.resolve() if resume_manifest_path else None),
        allow_formal_publication=allow_formal_publication,
    )
    spec = {
        "pipelineId": pipeline_id,
        "version": pipeline_version,
        "steps": [{"id": step_id, "handler": step_id}],
    }
    result = PipelineRunner(spec, context).run({step_id: handler})
    register_run(
        root,
        run_id=run_id,
        asset_type=pipeline_id,
        manifest_path=output_manifest,
        idempotency_key=idempotency_key(pipeline_id, pipeline_version, step_id, context.parameters, context.parameters),
        inputs=dict(context.parameters),
        outputs=result,
        source_snapshot_id=str(result.get("sourceSnapshotId") or result.get("source_snapshot_id") or "").strip() or None,
        replayable=bool(result.get("idempotentReplay") or result.get("replayable") or False),
        allow_formal_publication=allow_formal_publication,
    )
    return result
