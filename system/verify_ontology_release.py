"""Verify a non-active ontology release before moving the release pointer."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from pipeline.contracts import connect_readonly, resolve_artifact_path
from rdflib import Dataset, Graph, URIRef
from rdflib.namespace import OWL, RDF
from replay_owl_rl import apply_rules
from semantic_packages import verify_package_registry
from semantic_registry import validate_registry_against_ontology
from standard_processors import run_independent_processors
from verify_standard_semantic_ci import run_sparql_contracts

ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parent
STANDARD_ROOT = PROJECT_ROOT / "standards"
DEFAULT_SPEC = STANDARD_ROOT / "ontology-version-v2.json"
TARGET = ROOT / "data" / "canonical_semantic.sqlite3"


def load_json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON object required: {path}")
    return value


def run(spec_path: Path) -> dict[str, object]:
    spec = load_json(spec_path)
    failures: list[str] = []
    resolved = {
        key: STANDARD_ROOT / str(spec[key])
        for key in ("namespaceFile", "ontologyFile", "vocabularyFile", "shaclFile", "contextFile", "rdfDatasetManifest", "assetManifest")
    }
    for key, path in resolved.items():
        if not path.exists():
            failures.append(f"ASSET_MISSING:{key}:{path}")

    ontology = Graph()
    vocabulary = Graph()
    shacl = Graph()
    if not failures:
        try:
            ontology.parse(str(resolved["ontologyFile"]), format="turtle")
            vocabulary.parse(str(resolved["vocabularyFile"]), format="turtle")
            shacl.parse(str(resolved["shaclFile"]), format="turtle")
            json.loads(resolved["contextFile"].read_text(encoding="utf-8"))
            load_json(resolved["rdfDatasetManifest"])
            load_json(resolved["assetManifest"])
        except Exception as exc:
            failures.append(f"ASSET_PARSE_FAILED:{exc}")

    version_iri = URIRef(str(spec.get("versionIri") or ""))
    if version_iri and (None, OWL.versionIRI, version_iri) not in ontology:
        failures.append("VERSION_IRI_NOT_DECLARED_BY_ONTOLOGY")
    registry_errors = validate_registry_against_ontology(resolved["ontologyFile"])
    failures.extend(registry_errors)
    package_contract = verify_package_registry()
    failures.extend(str(item) for item in package_contract.get("failures", []))

    migration_path = STANDARD_ROOT / str(spec.get("migrationManifest") or "")
    migration = load_json(migration_path) if migration_path.exists() else {}
    if migration.get("fromVersion") != "enterprise-operations-ontology/v1" or migration.get("toVersion") != spec.get("version"):
        failures.append("MIGRATION_MANIFEST_VERSION_MISMATCH")
    if not isinstance(migration.get("compatibilityMap"), dict) or not migration["compatibilityMap"]:
        failures.append("MIGRATION_COMPATIBILITY_MAP_EMPTY")

    independent: dict[str, object] = {"status": "not_run", "gateStatus": "not_run"}
    replay: dict[str, object] = {"status": "not_run"}
    sparql: dict[str, object] = {"status": "not_run"}
    if TARGET.exists():
        db = connect_readonly(TARGET)
        projection = db.execute("SELECT * FROM canonical_projection_run WHERE status='completed' ORDER BY created_at DESC LIMIT 1").fetchone()
        if projection is None:
            failures.append("CANONICAL_PROJECTION_MISSING")
        else:
            manifest = json.loads(projection["manifest_json"])
            run_dir = ROOT / "canonical-runs" / str(projection["run_id"])
            reasoning = resolve_artifact_path(manifest["artifacts"].get("reasoningTrig") or manifest["artifacts"]["trig"], run_dir)
            dataset = Dataset()
            dataset.parse(reasoning, format="trig")
            data_graph = Graph()
            for subject, predicate, obj, _context in dataset.quads((None, None, None, None)):
                data_graph.add((subject, predicate, obj))
            independent = run_independent_processors(data_graph, resolved["ontologyFile"], resolved["shaclFile"])
            if independent.get("gateStatus") != "pass":
                failures.append("INDEPENDENT_STANDARD_PROCESSOR_GATE_" + str(independent.get("status", "unknown")).upper())
            combined = Graph()
            for triple in ontology:
                combined.add(triple)
            for triple in data_graph:
                combined.add(triple)
            inferred, _support, iterations = apply_rules(combined)
            forbidden = {
                (URIRef("https://semantic.local/ontology/action/ACTION_CREATE_DEFECT"), RDF.type, URIRef("https://semantic.local/ontology/Rule")),
            }
            if any(triple in inferred for triple in forbidden):
                failures.append("FORBIDDEN_OWL_RL_INFERENCE_PRESENT")
            replay = {"status": "passed", "inputTripleCount": len(combined), "inferredTripleCount": len(inferred), "iterations": iterations}
            sparql_manifest_path = PROJECT_ROOT / "sparql" / Path(str(spec["sparqlManifest"])).name
            contract = load_json(sparql_manifest_path)
            sparql, sparql_failures = run_sparql_contracts(dataset, contract)
            failures.extend(sparql_failures)
            jsonld_path = resolve_artifact_path(manifest["artifacts"].get("reasoningJsonLd") or manifest["artifacts"]["jsonld"], run_dir)
            try:
                Dataset().parse(jsonld_path, format="json-ld")
            except Exception as exc:
                failures.append(f"JSONLD_REGRESSION_FAILED:{exc}")
        db.close()

    return {
        "status": "PASS" if not failures else "FAIL",
        "version": spec.get("version"),
        "versionIri": spec.get("versionIri"),
        "assets": {key: str(path) for key, path in resolved.items()},
        "registryOwl": {"status": "passed" if not registry_errors else "failed", "failures": registry_errors},
        "semanticPackages": package_contract,
        "migration": migration,
        "replay": replay,
        "independentStandardProcessors": independent,
        "sparql": sparql,
        "failures": failures,
        "sourceWrite": False,
        "formalPublication": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify an ontology release without activating it")
    parser.add_argument("--spec", type=Path, default=DEFAULT_SPEC)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = run(args.spec.resolve())
    if args.output:
        args.output.resolve().parent.mkdir(parents=True, exist_ok=True)
        args.output.resolve().write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result["status"] == "PASS" else 1)


if __name__ == "__main__":
    main()
