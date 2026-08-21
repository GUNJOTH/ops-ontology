"""Validate first-class Canonical RDF semantic assets and the local runtime.

The gate is local and read-only with respect to source systems. It validates
the ontology, SKOS vocabulary, SHACL asset, RDF Dataset contract, SPARQL
contracts, TriG/JSON-LD projection, bounded OWL 2 RL replay, and provenance.
Controlled identity conflicts remain visible in the report; CI never resolves
them silently.
"""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

from rdflib import Dataset, Graph, URIRef
from rdflib.namespace import OWL, RDF

from build_canonical_semantic_model import STANDARD_ROOT
from replay_owl_rl import persist_inference
from semantic_registry import validate_registry_contract
from semantic_namespaces import validate_namespace_contract
from verify_canonical_semantic_model import verify as verify_canonical


ROOT = Path(__file__).resolve().parent
SPARQL_ROOT = ROOT.parent / "sparql"
CONTRACT_ROOT = ROOT.parent / "contracts"
TARGET = ROOT / "data" / "canonical_semantic.sqlite3"
OUTPUT = ROOT / "data" / "standard_semantic_ci_report.json"


def load_json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON object required: {path}")
    return value


def validate_first_class_assets() -> tuple[dict[str, object], list[str]]:
    failures: list[str] = []
    status: dict[str, object] = {"status": "not_run", "assets": {}}
    try:
        version_spec = load_json(STANDARD_ROOT / "ontology-version.json")
        namespace_spec = load_json(STANDARD_ROOT / str(version_spec.get("namespaceFile", "namespace.json")))
        asset_manifest = load_json(STANDARD_ROOT / str(version_spec["assetManifest"]))
        dataset_contract = load_json(STANDARD_ROOT / str(version_spec["rdfDatasetManifest"]))
        sparql_manifest = load_json(STANDARD_ROOT / str(version_spec["sparqlManifest"]))
        required_assets = {
            "namespace": STANDARD_ROOT / str(version_spec.get("namespaceFile", "namespace.json")),
            "ontology": STANDARD_ROOT / str(version_spec["ontologyFile"]),
            "vocabulary": STANDARD_ROOT / str(version_spec["vocabularyFile"]),
            "shacl": STANDARD_ROOT / str(version_spec["shaclFile"]),
            "context": STANDARD_ROOT / str(version_spec["contextFile"]),
            "rdfDataset": STANDARD_ROOT / str(version_spec["rdfDatasetManifest"]),
            "assetManifest": STANDARD_ROOT / str(version_spec["assetManifest"]),
            "sparqlManifest": STANDARD_ROOT / str(version_spec["sparqlManifest"]),
        }
        missing = [name for name, path in required_assets.items() if not path.exists()]
        if missing:
            failures.append("FIRST_CLASS_ASSET_MISSING:" + ",".join(missing))
        namespace_keys = (
            "baseNamespace",
            "ontologyNamespace",
            "resourceNamespace",
            "sourceNamespace",
            "graphNamespace",
            "contractNamespace",
        )
        if any(not str(namespace_spec.get(key) or "").startswith("https://") for key in namespace_keys):
            failures.append("NAMESPACE_CONTRACT_INVALID")
        version_iri = str(version_spec.get("versionIri") or "")
        ontology_namespace = str(namespace_spec.get("ontologyNamespace") or "")
        if not version_iri.startswith(ontology_namespace):
            failures.append("VERSION_IRI_NAMESPACE_MISMATCH")
        contract_status: dict[str, object] = {}
        for contract_name in ("world_context.schema.json", "explain.schema.json"):
            contract_path = CONTRACT_ROOT / contract_name
            try:
                contract = load_json(contract_path)
                contract_id = str(contract.get("$id") or "")
                valid_flags = all(
                    isinstance(contract.get("properties", {}).get(flag), dict)
                    and contract["properties"][flag].get("const") is False
                    for flag in ("sourceWrite", "formalPublication")
                )
                if not contract_id.startswith(str(namespace_spec.get("contractNamespace") or "")):
                    failures.append(f"CONTRACT_NAMESPACE_INVALID:{contract_name}")
                if not valid_flags:
                    failures.append(f"CONTRACT_UNSAFE_FLAGS_INVALID:{contract_name}")
                contract_status[contract_name] = {"status": "passed" if contract_id and valid_flags else "failed", "id": contract_id}
            except Exception as exc:
                failures.append(f"CONTRACT_PARSE_FAILED:{contract_name}:{exc}")
                contract_status[contract_name] = {"status": "failed", "error": str(exc)}
        rules_text = (ROOT.parent / "contracts" / "unified_device_semantics.yaml").read_text(encoding="utf-8")
        if 'source_write_policy: "never"' not in rules_text:
            failures.append("DEVICE_CONTRACT_SOURCE_WRITE_POLICY_INVALID")
        contract_status["unified_device_semantics.yaml"] = {
            "status": "passed" if 'source_write_policy: "never"' in rules_text else "failed"
        }
        query_rows = sparql_manifest.get("queries")
        if not isinstance(query_rows, list) or not query_rows:
            failures.append("SPARQL_MANIFEST_EMPTY")
        else:
            for item in query_rows:
                if not isinstance(item, dict) or not item.get("file"):
                    failures.append("SPARQL_MANIFEST_ENTRY_INVALID")
                    continue
                if not (SPARQL_ROOT / str(item["file"])).exists():
                    failures.append(f"SPARQL_QUERY_MISSING:{item['file']}")
        graph_kinds = [str(item.get("kind")) for item in dataset_contract.get("namedGraphs", []) if isinstance(item, dict)]
        for required_kind in ("ontology", "source", "derived", "provenance", "inference"):
            if required_kind not in graph_kinds:
                failures.append(f"RDF_DATASET_GRAPH_CONTRACT_MISSING:{required_kind}")
        status = {
            "status": "passed" if not failures else "failed",
            "version": version_spec.get("version"),
            "assetManifest": str(STANDARD_ROOT / str(version_spec["assetManifest"])),
            "rdfDatasetManifest": str(STANDARD_ROOT / str(version_spec["rdfDatasetManifest"])),
            "sparqlManifest": str(STANDARD_ROOT / str(version_spec["sparqlManifest"])),
            "assetCount": len(asset_manifest.get("assets", {})),
            "namespace": namespace_spec,
            "queryCount": len(query_rows) if isinstance(query_rows, list) else 0,
            "graphKinds": graph_kinds,
            "contracts": contract_status,
        }
    except Exception as exc:
        failures.append(f"FIRST_CLASS_ASSET_CONTRACT_FAILED:{exc}")
        status = {"status": "failed", "error": str(exc)}
    return status, failures


def run_sparql_contracts(dataset: Dataset, manifest: dict[str, object]) -> tuple[dict[str, object], list[str]]:
    failures: list[str] = []
    rows: list[dict[str, object]] = []
    union = Graph()
    for subject, predicate, obj, _context in dataset.quads((None, None, None, None)):
        union.add((subject, predicate, obj))
    for item in manifest.get("queries", []):
        if not isinstance(item, dict):
            continue
        query_id = str(item.get("id") or item.get("file"))
        query_path = SPARQL_ROOT / str(item.get("file"))
        try:
            query_text = query_path.read_text(encoding="utf-8")
            if re.search(r"\b(INSERT|DELETE|LOAD|CLEAR|CREATE|DROP|COPY|MOVE|ADD|SERVICE)\b", query_text, re.IGNORECASE):
                raise ValueError("only read-only SELECT/ASK contracts are allowed")
            result = list(union.query(query_text))
            rows.append({"id": query_id, "file": str(query_path), "status": "passed", "rowCount": len(result)})
        except Exception as exc:
            failures.append(f"SPARQL_QUERY_FAILED:{query_id}:{exc}")
            rows.append({"id": query_id, "file": str(query_path), "status": "failed", "error": str(exc)})
    return {"status": "passed" if not failures else "failed", "queries": rows}, failures


def run() -> dict[str, object]:
    failures: list[str] = []
    registry_failures = validate_registry_contract()
    failures.extend(registry_failures)
    namespace_contract_failures = validate_namespace_contract()
    failures.extend(namespace_contract_failures)
    asset_contract, asset_failures = validate_first_class_assets()
    failures.extend(asset_failures)
    parsed: dict[str, object] = {}
    namespace_spec: dict[str, object] = {}
    try:
        version_spec = load_json(STANDARD_ROOT / "ontology-version.json")
        namespace_spec = load_json(STANDARD_ROOT / str(version_spec.get("namespaceFile", "namespace.json")))
        standard_assets = [
            ("ontology", STANDARD_ROOT / str(version_spec["ontologyFile"]), "turtle"),
            ("vocabularies", STANDARD_ROOT / str(version_spec["vocabularyFile"]), "turtle"),
            ("shacl", STANDARD_ROOT / str(version_spec["shaclFile"]), "turtle"),
        ]
    except Exception as exc:
        standard_assets = []
        failures.append(f"VERSION_SPEC_LOAD_FAILED:{exc}")
    shacl_graph = None
    ontology_graph = None
    for name, path, fmt in standard_assets:
        try:
            graph = Graph()
            graph.parse(str(path), format=fmt)
            if name == "shacl":
                shacl_graph = graph
            if name == "ontology":
                ontology_graph = graph
            parsed[name] = len(graph) > 0
            if not parsed[name]:
                failures.append(f"{name.upper()}_EMPTY")
        except Exception as exc:
            parsed[name] = False
            failures.append(f"{name.upper()}_PARSE_FAILED:{exc}")
    required_shape_classes = {
        "Device", "Location", "IdentityAssertion", "BusinessEvent", "Defect",
        "WorkOrder", "Rule", "Fact", "ActionPlan",
    }
    if shacl_graph is not None:
        shacl_ns = "http://www.w3.org/ns/shacl#"
        shape_targets = {
            str(target).rsplit("/", 1)[-1]
            for shape in shacl_graph.subjects(RDF.type, URIRef(shacl_ns + "NodeShape"))
            for target in shacl_graph.objects(shape, URIRef(shacl_ns + "targetClass"))
        }
        parsed["shaclShapeCount"] = len(shape_targets)
        missing_shapes = sorted(required_shape_classes - shape_targets)
        if missing_shapes:
            failures.append("SHACL_REQUIRED_SHAPES_MISSING:" + ",".join(missing_shapes))
    if ontology_graph is not None:
        context_payload = load_json(STANDARD_ROOT / "context.jsonld")
        context = context_payload.get("@context", {})
        ontology_namespace = str(namespace_spec.get("ontologyNamespace") or "")
        context_iris: set[str] = set()
        if isinstance(context, dict):
            for key, value in context.items():
                if key in {"ex", "rdf", "rdfs", "owl", "prov", "skos", "sh"}:
                    continue
                mapping = value.get("@id") if isinstance(value, dict) else value
                if isinstance(mapping, str) and mapping.startswith("ex:"):
                    context_iris.add(ontology_namespace + mapping[3:])
        property_iris = {
            str(item)
            for property_type in (OWL.ObjectProperty, OWL.DatatypeProperty)
            for item in ontology_graph.subjects(RDF.type, property_type)
        }
        missing_context = sorted(item for item in property_iris if item not in context_iris)
        parsed["jsonldContextTermCoverage"] = {
            "ontologyPropertyCount": len(property_iris),
            "mappedPropertyCount": len(property_iris) - len(missing_context),
            "missing": missing_context,
        }
        if missing_context:
            failures.append("JSONLD_CONTEXT_TERMS_MISSING:" + ",".join(missing_context))
    try:
        context = json.loads((STANDARD_ROOT / "context.jsonld").read_text(encoding="utf-8"))
        parsed["jsonldContext"] = isinstance(context.get("@context"), dict)
        if not parsed["jsonldContext"]:
            failures.append("JSONLD_CONTEXT_INVALID")
    except Exception as exc:
        parsed["jsonldContext"] = False
        failures.append(f"JSONLD_CONTEXT_PARSE_FAILED:{exc}")

    inference = persist_inference(TARGET) if TARGET.exists() else None
    canonical = verify_canonical(TARGET)
    failures.extend(str(item) for item in canonical.get("failures", []))

    sparql: dict[str, object] = {"status": "not_run", "deviceCount": 0, "queries": []}
    if TARGET.exists():
        db = sqlite3.connect(f"file:{TARGET.resolve()}?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        run_row = db.execute("SELECT * FROM canonical_projection_run WHERE status='completed' ORDER BY created_at DESC LIMIT 1").fetchone()
        if run_row:
            manifest = json.loads(run_row["manifest_json"])
            dataset = Dataset()
            # Full source identity projection is streamed and may contain
            # millions of Device seeds.  Run SPARQL contracts and bounded RL
            # checks against the semantic reasoning artifact; the full RDF
            # file itself is checked by verify_canonical's stream gate.
            reasoning_trig = manifest["artifacts"].get("reasoningTrig") or manifest["artifacts"]["trig"]
            dataset.parse(str(reasoning_trig), format="trig")
            if inference and inference.get("artifacts", {}).get("trig"):
                dataset.parse(str(inference["artifacts"]["trig"]), format="trig")
            union = Graph()
            for subject, predicate, obj, _context in dataset.quads((None, None, None, None)):
                union.add((subject, predicate, obj))
            ontology_namespace = str(namespace_spec.get("ontologyNamespace") or "https://semantic.local/ontology/")
            query = f"SELECT (COUNT(?s) AS ?count) WHERE {{ ?s a <{ontology_namespace}Device> . }}"
            if manifest.get("streamingProjection"):
                sparql["deviceCount"] = int((manifest.get("canonicalScope") or {}).get("sourceIdentityDeviceCount") or 0)
                sparql["deviceCountScope"] = "full-canonical-source-identity"
            else:
                rows = list(union.query(query))
                sparql["deviceCount"] = int(rows[0][0]) if rows else 0
                sparql["deviceCountScope"] = "reasoning-graph"
            if sparql["deviceCount"] <= 0:
                failures.append("SPARQL_DEVICE_QUERY_EMPTY")
            try:
                sparql_manifest = load_json(STANDARD_ROOT / str(load_json(STANDARD_ROOT / "ontology-version.json")["sparqlManifest"]))
                contract_result, contract_failures = run_sparql_contracts(dataset, sparql_manifest)
                sparql.update(contract_result)
                failures.extend(contract_failures)
            except Exception as exc:
                failures.append(f"SPARQL_CONTRACT_LOAD_FAILED:{exc}")
            graph_kinds = {str(row[0]) for row in db.execute("SELECT DISTINCT graph_kind FROM canonical_graph WHERE run_id=?", (run_row["run_id"],)).fetchall()}
            for required_kind in ("ontology", "source", "derived", "provenance"):
                if required_kind not in graph_kinds:
                    failures.append(f"RDF_DATASET_GRAPH_MISSING:{required_kind}")
            jsonld = Dataset()
            reasoning_jsonld = manifest["artifacts"].get("reasoningJsonLd") or manifest["artifacts"]["jsonld"]
            jsonld.parse(str(reasoning_jsonld), format="json-ld")
            parsed["canonicalJsonLd"] = len(list(jsonld.quads((None, None, None, None)))) > 0
            if not parsed["canonicalJsonLd"]:
                failures.append("CANONICAL_JSONLD_EMPTY")
        else:
            failures.append("CANONICAL_PROJECTION_MISSING")
        db.close()

    result = {
        "status": "PASS" if not failures else "FAIL",
        "parsed": parsed,
        "firstClassAssets": asset_contract,
        "canonical": canonical,
        "owlRlReplay": inference,
        "sparql": sparql,
        "semanticRegistry": {
            "status": "passed" if not registry_failures else "failed",
            "failureCount": len(registry_failures),
            "failures": registry_failures,
        },
        "namespaceContract": {
            "status": "passed" if not namespace_contract_failures else "failed",
            "failureCount": len(namespace_contract_failures),
            "failures": namespace_contract_failures,
        },
        "failures": failures,
        "sourceWrite": False,
        "formalPublication": False,
    }
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


if __name__ == "__main__":
    result = run()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result["status"] == "PASS" else 1)
