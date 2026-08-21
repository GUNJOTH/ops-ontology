"""Verify the Canonical RDF read-authority cutover coverage.

This is a read-only gate.  A partial projection is not treated as a success:
it returns PASS_WITH_REVIEW and identifies the compatibility-read population.
It never changes the source snapshot, semantic overlay, or Canonical Dataset.
"""
from __future__ import annotations

import json
import pathlib
import sqlite3
import sys

ROOT = pathlib.Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parent
DATA_DIR = ROOT / "data"
IDENTITY_ROOT = PROJECT_ROOT / "pilots" / "identity" / "results"
CANONICAL_DB = DATA_DIR / "canonical_semantic.sqlite3"


def latest_identity_db() -> pathlib.Path | None:
    for directory in sorted(IDENTITY_ROOT.glob("identity-layer-v1-*/"), reverse=True):
        database = directory / "identity_semantics.sqlite3"
        if (directory / "manifest.json").exists() and database.exists():
            return database
    return None


def main() -> int:
    identity_path = latest_identity_db()
    if identity_path is None or not CANONICAL_DB.exists():
        print(json.dumps({"status": "FAIL", "reason": "身份结果库或 Canonical RDF Dataset 不存在", "sourceWrite": False}, ensure_ascii=False, indent=2))
        return 1
    identity = sqlite3.connect(f"file:{identity_path.resolve()}?mode=ro", uri=True)
    canonical = sqlite3.connect(f"file:{CANONICAL_DB.resolve()}?mode=ro", uri=True)
    try:
        identity_count = int(identity.execute("SELECT count(*) FROM unified_device").fetchone()[0])
        run = canonical.execute(
            "SELECT run_id FROM canonical_projection_run WHERE status='completed' ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        if run is None:
            print(json.dumps({"status": "FAIL", "reason": "没有已完成的 Canonical RDF 投影", "sourceWrite": False}, ensure_ascii=False, indent=2))
            return 1
        manifest_row = canonical.execute(
            "SELECT manifest_json FROM canonical_projection_run WHERE run_id=?",
            (run[0],),
        ).fetchone()
        projected_count: int | None = None
        if manifest_row:
            try:
                manifest = json.loads(manifest_row[0] or "{}")
                scope = manifest.get("canonicalScope") if isinstance(manifest, dict) else None
                if isinstance(scope, dict) and scope.get("sourceIdentityDeviceCount") is not None:
                    projected_count = int(scope["sourceIdentityDeviceCount"])
            except (TypeError, json.JSONDecodeError, ValueError):
                pass
        if projected_count is None:
            projected_count = int(canonical.execute(
                "SELECT count(DISTINCT subject_iri) FROM canonical_statement WHERE run_id=? AND predicate_iri=? AND object_iri=?",
                (run[0], "http://www.w3.org/1999/02/22-rdf-syntax-ns#type", "https://semantic.local/ontology/Device"),
            ).fetchone()[0])
        coverage = projected_count / identity_count if identity_count else 0.0
        status = "PASS" if identity_count and projected_count == identity_count else "PASS_WITH_REVIEW"
        print(json.dumps({
            "status": status,
            "authority": "Canonical RDF Dataset",
            "canonicalRunId": run[0],
            "projectedDeviceCount": projected_count,
            "identityDeviceCount": identity_count,
            "coverage": round(coverage, 6),
            "compatibilityDeviceCount": max(0, identity_count - projected_count),
            "sourceWrite": False,
            "formalPublication": False,
        }, ensure_ascii=False, indent=2))
        return 0
    finally:
        canonical.close()
        identity.close()


if __name__ == "__main__":
    sys.exit(main())
