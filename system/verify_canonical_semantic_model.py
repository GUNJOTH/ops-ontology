"""Verify the latest canonical RDF projection and its safety gates."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

try:
    from rdflib import Dataset, Graph
except ImportError as exc:  # pragma: no cover
    raise SystemExit("缺少 rdflib，请安装 system/requirements.txt") from exc


ROOT = Path(__file__).resolve().parent
TARGET = ROOT / "data" / "canonical_semantic.sqlite3"
SOURCE = ROOT / "data" / "unified_semantics.sqlite3"
OUTPUT = ROOT / "data" / "canonical_semantic_verification.json"


def verify_streamed_artifacts(manifest: dict[str, object]) -> dict[str, object]:
    """Validate large streamed RDF artifacts without loading them into rdflib."""
    artifacts = manifest.get("artifacts") or {}
    trig_path = Path(str(artifacts.get("trig") or ""))
    jsonld_path = Path(str(artifacts.get("jsonld") or ""))
    expected = int((manifest.get("canonicalScope") or {}).get("sourceIdentityDeviceCount") or 0)
    trig_devices = 0
    jsonld_devices = 0
    trig_bytes = 0
    jsonld_bytes = 0
    trig_tail = ""
    trig_ok = trig_path.is_file()
    jsonld_ok = jsonld_path.is_file()
    if trig_ok:
        trig_bytes = trig_path.stat().st_size
        snapshots = [str(value) for value in ((manifest.get("canonicalScope") or {}).get("sourceIdentitySnapshots") or [])]
        identity_markers = {f"https://semantic.local/graph/source/{snapshot}" for snapshot in snapshots}
        in_identity_graph = False
        with trig_path.open("r", encoding="utf-8", errors="strict") as handle:
            for line in handle:
                stripped = line.strip()
                if stripped.endswith(" {"):
                    in_identity_graph = any(marker in stripped for marker in identity_markers)
                if in_identity_graph and " a <https://semantic.local/ontology/Device> ;" in line:
                    trig_devices += 1
                if in_identity_graph and stripped == "}":
                    in_identity_graph = False
                trig_tail = line.strip() or trig_tail
        trig_ok = trig_tail.endswith("}") and trig_devices == expected
    if jsonld_ok:
        jsonld_bytes = jsonld_path.stat().st_size
        with jsonld_path.open("r", encoding="utf-8", errors="strict") as handle:
            for line in handle:
                jsonld_devices += line.count('"type":"Device","canonicalKey"')
        jsonld_ok = jsonld_devices == expected
    return {
        "status": "passed" if trig_ok and jsonld_ok else "failed",
        "expectedDeviceCount": expected,
        "trigDeviceCount": trig_devices,
        "jsonldDeviceCount": jsonld_devices,
        "trigBytes": trig_bytes,
        "jsonldBytes": jsonld_bytes,
        "trig": trig_ok,
        "jsonld": jsonld_ok,
        "mode": "streamed-source-identity",
    }


def source_quality(source: Path) -> dict[str, object]:
    if not source.exists():
        return {"status": "missing", "failures": ["SEMANTIC_RUNTIME_MISSING"]}
    db = sqlite3.connect(f"file:{source.resolve()}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA query_only=ON")

    def exists(name: str) -> bool:
        return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None

    quality = {
        "status": "ok",
        "objectCount": int(db.execute("SELECT count(*) FROM semantic_object_instance WHERE status IN ('accepted','observed','derived','current')").fetchone()[0]) if exists("semantic_object_instance") else 0,
        "identityAssertionCount": int(db.execute("SELECT count(*) FROM semantic_identity_assertion WHERE status='accepted' AND lifecycle_status='active'").fetchone()[0]) if exists("semantic_identity_assertion") else 0,
        "identityConflictCount": int(db.execute("SELECT count(*) FROM semantic_identity_conflict WHERE status='isolated'").fetchone()[0]) if exists("semantic_identity_conflict") else 0,
        "identityReviewPendingCount": int(db.execute("SELECT count(*) FROM semantic_identity_review WHERE queue_status='pending'").fetchone()[0]) if exists("semantic_identity_review") else 0,
        "sourceNamespaces": [str(row[0]) for row in db.execute("SELECT DISTINCT source_namespace FROM semantic_object_instance WHERE source_namespace IS NOT NULL AND trim(source_namespace)<>'' ORDER BY source_namespace").fetchall()] if exists("semantic_object_instance") else [],
    }
    db.close()
    return quality


def verify(target: Path = TARGET) -> dict[str, object]:
    failures: list[str] = []
    if not target.exists():
        failures.append("CANONICAL_DB_MISSING")
        result = {"status": "FAIL", "failures": failures, "sourceWrite": False, "formalPublication": False}
        OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return result

    db = sqlite3.connect(f"file:{target.resolve()}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    run = db.execute("SELECT * FROM canonical_projection_run ORDER BY created_at DESC LIMIT 1").fetchone()
    if run is None:
        failures.append("CANONICAL_RUN_MISSING")
    else:
        if run["status"] != "completed":
            failures.append("CANONICAL_RUN_NOT_COMPLETED")
        if int(run["statement_count"] or 0) <= 0:
            failures.append("CANONICAL_STATEMENTS_EMPTY")
        if int(run["graph_count"] or 0) < 4:
            failures.append("CANONICAL_GRAPH_SET_INCOMPLETE")
        if int(run["validation_error_count"] or 0) != 0:
            failures.append("SHACL_VALIDATION_ERRORS")
        if int(run["source_write"] or 0) != 0 or int(run["formal_publication"] or 0) != 0:
            failures.append("CANONICAL_UNSAFE_FLAGS")

    artifacts: dict[str, str] = {}
    parsed = {"trig": False, "jsonld": False, "ontology": False, "shacl": False}
    source_quality_report = source_quality(SOURCE)
    inference = None
    if run:
        manifest = json.loads(run["manifest_json"])
        artifacts = dict(manifest.get("artifacts") or {})
        manifest_streamed = bool(manifest.get("streamingProjection"))
        if manifest_streamed:
            streamed = verify_streamed_artifacts(manifest)
            parsed["streamedArtifacts"] = streamed
            parsed["trig"] = bool(streamed["trig"])
            parsed["jsonld"] = bool(streamed["jsonld"])
            if not parsed["trig"]:
                failures.append("TRIG_STREAM_VALIDATION_FAILED")
            if not parsed["jsonld"]:
                failures.append("JSONLD_STREAM_VALIDATION_FAILED")
            # Parse the bounded reasoning artifact fully; the large source
            # identity graph is validated by the deterministic stream gate.
            try:
                reasoning = Dataset()
                reasoning.parse(str(artifacts.get("reasoningTrig") or artifacts["trig"]), format="trig")
                parsed["reasoningTrig"] = len(list(reasoning.quads((None, None, None, None)))) > 0
            except Exception as exc:  # pragma: no cover
                failures.append(f"REASONING_TRIG_PARSE_FAILED:{exc}")
            try:
                reasoning_jsonld = Dataset()
                reasoning_jsonld.parse(str(artifacts.get("reasoningJsonLd") or artifacts["jsonld"]), format="json-ld")
                parsed["reasoningJsonLd"] = len(list(reasoning_jsonld.quads((None, None, None, None)))) > 0
            except Exception as exc:  # pragma: no cover
                failures.append(f"REASONING_JSONLD_PARSE_FAILED:{exc}")
        else:
            try:
                dataset = Dataset()
                dataset.parse(artifacts["trig"], format="trig")
                parsed["trig"] = len(list(dataset.quads((None, None, None, None)))) > 0
            except Exception as exc:  # pragma: no cover - exact parser errors depend on rdflib version
                failures.append(f"TRIG_PARSE_FAILED:{exc}")
            try:
                jsonld_dataset = Dataset()
                jsonld_dataset.parse(artifacts["jsonld"], format="json-ld")
                parsed["jsonld"] = len(list(jsonld_dataset.quads((None, None, None, None)))) > 0
                if not parsed["jsonld"]:
                    failures.append("JSONLD_EMPTY")
            except Exception as exc:  # pragma: no cover
                failures.append(f"JSONLD_PARSE_FAILED:{exc}")
        inference_db = db
        if inference_db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='canonical_inference_run'").fetchone():
            inference_row = inference_db.execute("SELECT * FROM canonical_inference_run ORDER BY created_at DESC LIMIT 1").fetchone()
            inference = dict(inference_row) if inference_row else None
            if inference is None or inference["input_projection_run_id"] != run["run_id"] or inference["status"] != "completed":
                failures.append("OWL_RL_REPLAY_NOT_COMPLETED")
            elif int(inference["source_write"] or 0) != 0 or int(inference["formal_publication"] or 0) != 0:
                failures.append("OWL_RL_REPLAY_UNSAFE_FLAGS")
        else:
            failures.append("OWL_RL_REPLAY_TABLE_MISSING")
    try:
        ontology = Graph()
        standards_root = ROOT.parent / "standards"
        ontology.parse(str(standards_root / "ontology.ttl"), format="turtle")
        parsed["ontology"] = len(ontology) > 0
        shapes = Graph()
        version_spec = json.loads((standards_root / "ontology-version.json").read_text(encoding="utf-8"))
        shacl_file = str(version_spec.get("shaclFile", "enterprise-operations.shacl.ttl"))
        shapes.parse(str(standards_root / shacl_file), format="turtle")
        parsed["shacl"] = len(shapes) > 0
    except Exception as exc:  # pragma: no cover
        failures.append(f"STANDARD_ASSET_PARSE_FAILED:{exc}")

    result = {
        "status": "PASS" if not failures else "FAIL",
        "latestRun": dict(run) if run else None,
        "artifacts": artifacts,
        "parsed": parsed,
        "statementCount": int(run["statement_count"] or 0) if run else 0,
        "provenanceCount": int(json.loads(run["manifest_json"]).get("provenanceCoverage", {}).get("coveredStatements", 0)) if run else 0,
        "provenanceCoverage": json.loads(json.loads(run["manifest_json"]).get("provenanceCoverage", "{}")) if run and isinstance(json.loads(run["manifest_json"]).get("provenanceCoverage"), str) else (json.loads(run["manifest_json"]).get("provenanceCoverage") if run else {}),
        "sourceQuality": source_quality_report,
        "owlRlReplay": inference,
        "sourceWrite": False,
        "formalPublication": False,
        "failures": failures,
    }
    db.close()
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


if __name__ == "__main__":
    value = verify()
    print(json.dumps(value, ensure_ascii=False, indent=2))
    raise SystemExit(0 if value["status"] == "PASS" else 1)
