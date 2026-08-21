"""Unified run-asset inventory for canonical-runs, reports and pipeline-runs.

The index is a single JSON file under ``system/reports/run_asset_index.json``.
It records enough provenance to answer:

- what was the run_id?
- which manifest describes the run?
- what were the inputs and outputs?
- which source snapshot did it depend on?
- is it replayable?
- is it inside the safe source-read-only / local-publication boundary?
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from .contracts import canonical_json, content_hash, utc_now, write_json_atomic

ASSET_INDEX_NAME = "run_asset_index.json"


def default_asset_index(root: Path) -> Path:
    return root / "reports" / ASSET_INDEX_NAME


def load_asset_index(path: Path | None = None) -> dict[str, Any]:
    if path is None:
        path = default_asset_index(Path(__file__).resolve().parents[1])
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"run asset index must be an object: {path}")
    return payload


def register_run(
    root: Path,
    *,
    run_id: str,
    asset_type: str,
    manifest_path: Path,
    idempotency_key: str,
    inputs: Mapping[str, Any],
    outputs: Mapping[str, Any],
    source_snapshot_id: str | None = None,
    replayable: bool = False,
    allow_formal_publication: bool = False,
) -> dict[str, Any]:
    """Append or replace one run record in the shared asset index."""
    index_path = default_asset_index(root)
    index = load_asset_index(index_path)
    output_mapping = dict(outputs)
    unsafe = [
        key
        for key in ("sourceWrite", "source_write", "formalPublication", "formal_publication")
        if output_mapping.get(key) is True or output_mapping.get(key) == 1
    ]
    if unsafe and not (allow_formal_publication and any(key.startswith("formal") for key in unsafe)):
        raise ValueError(f"cannot register unsafe run {run_id}: {','.join(unsafe)}")
    record = {
        "runId": run_id,
        "assetType": asset_type,
        "manifestPath": str(manifest_path),
        "idempotencyKey": idempotency_key,
        "inputs": dict(inputs),
        "outputs": output_mapping,
        "sourceSnapshotId": source_snapshot_id,
        "replayable": bool(replayable),
        "safeBoundary": not unsafe,
        "registeredAt": utc_now(),
        "contentHash": content_hash(run_id, asset_type, canonical_json(inputs), canonical_json(outputs)),
    }
    index[run_id] = record
    write_json_atomic(index_path, index)
    return record


def summarize(root: Path) -> dict[str, Any]:
    """Return a compact summary of all registered runs."""
    index = load_asset_index(default_asset_index(root))
    return {
        "total": len(index),
        "assetTypes": sorted({str(item.get("assetType") or "") for item in index.values()}),
        "runs": [
            {
                "runId": run_id,
                "assetType": item.get("assetType"),
                "manifestPath": item.get("manifestPath"),
                "sourceSnapshotId": item.get("sourceSnapshotId"),
                "replayable": item.get("replayable"),
                "safeBoundary": item.get("safeBoundary"),
            }
            for run_id, item in sorted(index.items())
        ],
    }
