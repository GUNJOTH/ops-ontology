"""Write a read-only operational report for the bounded semantic test run."""
from __future__ import annotations

import argparse
import json
import pathlib
import sqlite3
import sys
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parent
DEFAULT_OUTPUT = ROOT / "reports" / "semantic-monitoring-5000.json"


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_json(path: pathlib.Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def readonly(path: pathlib.Path) -> sqlite3.Connection:
    db = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA query_only=ON")
    return db


def load_duckdb():
    abi = f"cp{sys.version_info.major}{sys.version_info.minor}"
    for dependency_dir in (PROJECT_ROOT / ".deps", PROJECT_ROOT / "backend" / ".deps"):
        candidates = list((dependency_dir / "duckdb").glob(f"*{abi}-win_amd64.pyd")) if (dependency_dir / "duckdb").exists() else []
        candidates.extend(dependency_dir.glob(f"_*{abi}-win_amd64.pyd"))
        if candidates:
            sys.path.insert(0, str(dependency_dir))
            import duckdb  # type: ignore

            return duckdb
    raise RuntimeError(f"未找到适用于 Python {abi} 的 DuckDB 运行时")


def latest(path: pathlib.Path, pattern: str) -> pathlib.Path | None:
    values = sorted(path.glob(pattern), key=lambda item: item.stat().st_mtime, reverse=True)
    return values[0] if values else None


def build(output: pathlib.Path) -> dict[str, object]:
    identity_path = latest(PROJECT_ROOT / "pilots" / "identity" / "results", "identity-layer-v1-*/identity_semantics.sqlite3")
    overlay_path = ROOT / "data" / "unified_semantics.sqlite3"
    canonical_path = ROOT / "data" / "canonical_semantic.sqlite3"
    analytics_path = ROOT / "data" / "semantic_analytics_v155.duckdb"
    identity = readonly(identity_path) if identity_path else None
    overlay = readonly(overlay_path)
    canonical = readonly(canonical_path)
    try:
        identity_count = int(identity.execute("SELECT count(*) FROM unified_device").fetchone()[0]) if identity else 0
        identity_review = int(identity.execute("SELECT count(*) FROM device_event WHERE link_status IN ('needs_review','blocked')").fetchone()[0]) if identity and identity.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='device_event'").fetchone() else 0
        overlay_counts = {
            "identityAssertions": int(overlay.execute("SELECT count(*) FROM semantic_identity_assertion").fetchone()[0]),
            "identityReviewPending": int(overlay.execute("SELECT count(*) FROM semantic_identity_review WHERE queue_status='pending'").fetchone()[0]),
            "facts": int(overlay.execute("SELECT count(*) FROM semantic_fact").fetchone()[0]),
            "events": int(overlay.execute("SELECT count(*) FROM semantic_event").fetchone()[0]),
            "currentStates": int(overlay.execute("SELECT count(*) FROM semantic_current_state").fetchone()[0]),
        }
        run = canonical.execute("SELECT * FROM canonical_projection_run WHERE status='completed' ORDER BY created_at DESC LIMIT 1").fetchone()
        canonical_manifest = json.loads(run["manifest_json"] or "{}") if run else {}
    finally:
        if identity:
            identity.close()
        overlay.close()
        canonical.close()

    duckdb = load_duckdb()
    duck = duckdb.connect(str(analytics_path), read_only=True)
    try:
        duck_metadata = {row[0]: row[1] for row in duck.execute("SELECT metadata_key,metadata_value FROM analytics_metadata").fetchall()}
        analytics = {
            "exists": True,
            "identityDeviceFacts": int(duck.execute("SELECT count(*) FROM semantic_device_identity_fact").fetchone()[0]),
            "legacyCandidateFacts": int(duck.execute("SELECT count(*) FROM semantic_candidate_fact").fetchone()[0]),
            "sourceSnapshotId": duck_metadata.get("source_snapshot_id"),
            "sourceWrite": duck_metadata.get("source_write"),
            "formalPublication": duck_metadata.get("formal_publication"),
        }
    finally:
        duck.close()

    gate_path = latest(ROOT / "reports", "semantic-release-gates*.json")
    gate = load_json(gate_path) if gate_path else {}
    active = load_json(ROOT / "data" / "semantic_release_active.json")
    canonical_run_id = str(run["run_id"]) if run else None
    source_identity_count = int((canonical_manifest.get("canonicalScope") or {}).get("sourceIdentityDeviceCount") or 0)
    checks = {
        "identitySnapshot": identity_count == 5000,
        "canonicalProjection": bool(run and int(run["validation_error_count"] or 0) == 0),
        "canonicalIdentityCoverage": source_identity_count == identity_count == 5000,
        "standardReleaseGates": gate.get("status") == "PASS" and gate.get("canonicalRunId") == canonical_run_id,
        "activeReleaseMatchesCanonical": active.get("canonicalRunId") == canonical_run_id,
        "duckdbAnalytics": analytics.get("identityDeviceFacts") == 5000,
        "sourceWriteBoundary": not bool(canonical_manifest.get("sourceWrite")) and not bool(active.get("sourceWrite")),
        "formalPublicationBoundary": not bool(canonical_manifest.get("formalPublication")) and not bool(active.get("formalPublication")),
    }
    result = {
        "schemaVersion": "semantic-monitoring-report-v1",
        "status": "PASS" if all(checks.values()) else "DEGRADED",
        "createdAt": now(),
        "testScope": "read-only bounded identity snapshot",
        "identity": {"database": str(identity_path) if identity_path else None, "deviceCount": identity_count, "reviewEvidenceCount": identity_review},
        "semanticOverlay": overlay_counts,
        "canonical": {"runId": canonical_run_id, "sourceIdentityDeviceCount": source_identity_count, "statementCount": int(run["statement_count"]) if run else 0, "graphCount": int(run["graph_count"]) if run else 0, "validationErrorCount": int(run["validation_error_count"]) if run else None},
        "analytics": analytics,
        "release": {"gateReport": str(gate_path) if gate_path else None, "activePointer": active},
        "checks": checks,
        "sourceWrite": False,
        "formalPublication": False,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a read-only semantic runtime monitoring report")
    parser.add_argument("--output", type=pathlib.Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    print(json.dumps(build(args.output.resolve()), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
