"""Load and validate the native Semantic Ontology Package registry.

Packages are the modular authoring boundary.  The composed Canonical RDF
assets remain the only runtime semantic authority; package manifests own
terms and package-local assets, while this module fails closed on overlap,
omission, broken dependencies, unsafe flags, or a failed composition gate.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from rdflib import Graph
from rdflib.namespace import OWL, RDF

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGES_ROOT = PROJECT_ROOT / "packages"
PACKAGE_REGISTRY = PACKAGES_ROOT / "manifest.json"
ONTOLOGY_NAMESPACE = "https://semantic.local/ontology/"
REQUIRED_ASSETS = (
    "ontology",
    "shapes",
    "vocabularies",
    "context",
    "mappings",
    "rules",
    "events",
    "stateMachines",
    "actions",
    "tests",
)


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON object required: {path}")
    return value


def load_package_registry(path: Path | None = None) -> dict[str, Any]:
    """Load the package registry without following remote RDF imports."""
    return _load_json(path or PACKAGE_REGISTRY)


def _package_specs(registry: dict[str, Any], root: Path) -> tuple[list[dict[str, Any]], list[str]]:
    failures: list[str] = []
    specs: list[dict[str, Any]] = []
    rows = registry.get("packages")
    if not isinstance(rows, list) or not rows:
        return [], ["PACKAGE_REGISTRY_EMPTY"]
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict) or not row.get("id") or not row.get("manifest"):
            failures.append("PACKAGE_ENTRY_INVALID")
            continue
        package_id = str(row["id"])
        if package_id in seen:
            failures.append(f"PACKAGE_DUPLICATE:{package_id}")
            continue
        seen.add(package_id)
        manifest_path = root / str(row["manifest"])
        if not manifest_path.exists():
            failures.append(f"PACKAGE_MANIFEST_MISSING:{package_id}")
            continue
        try:
            spec = _load_json(manifest_path)
        except Exception as exc:
            failures.append(f"PACKAGE_MANIFEST_INVALID:{package_id}:{exc}")
            continue
        if str(spec.get("id")) != package_id:
            failures.append(f"PACKAGE_ID_MISMATCH:{package_id}")
        spec["_manifest_path"] = manifest_path
        spec["_root"] = manifest_path.parent
        spec["_registry_entry"] = row
        specs.append(spec)
    return specs, failures


def _asset_path(spec: dict[str, Any], key: str) -> Path | None:
    value = (spec.get("assets") or {}).get(key)
    if not value:
        return None
    root = Path(spec["_root"]).resolve()
    path = (root / str(value)).resolve()
    try:
        path.relative_to(root)
    except ValueError:
        return None
    return path


def _load_active_ontology(path: Path) -> tuple[Graph | None, list[str]]:
    graph = Graph()
    try:
        graph.parse(str(path), format="turtle")
    except Exception as exc:
        return None, [f"PACKAGE_CANONICAL_ONTOLOGY_PARSE_FAILED:{exc}"]
    return graph, []


def _sha256(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_package_registry(
    *,
    registry_path: Path | None = None,
    ontology_path: Path | None = None,
) -> dict[str, Any]:
    """Verify package ownership against the active aggregate ontology."""
    registry_file = registry_path or PACKAGE_REGISTRY
    failures: list[str] = []
    try:
        registry = load_package_registry(registry_file)
    except Exception as exc:
        return {"status": "FAIL", "failures": [f"PACKAGE_REGISTRY_PARSE_FAILED:{exc}"]}

    if registry.get("schemaVersion") not in {"semantic-package-registry-v1", "semantic-package-registry-v2"}:
        failures.append("PACKAGE_REGISTRY_SCHEMA_INVALID")
    if registry.get("canonicalAssetMode") not in {"aggregate", "composed"}:
        failures.append("PACKAGE_CANONICAL_MODE_INVALID")
    if registry.get("sourceWrite") is not False or registry.get("formalPublication") is not False:
        failures.append("PACKAGE_UNSAFE_FLAGS")

    specs, spec_failures = _package_specs(registry, PACKAGES_ROOT)
    failures.extend(spec_failures)
    package_status: list[dict[str, Any]] = []
    class_owners: dict[str, str] = {}
    object_property_owners: dict[str, str] = {}
    datatype_property_owners: dict[str, str] = {}
    event_owners: dict[str, str] = {}

    for spec in specs:
        package_id = str(spec["id"])
        package_failures: list[str] = []
        if spec.get("schemaVersion") not in {"semantic-package-v1", "semantic-package-v2"}:
            package_failures.append("SCHEMA_INVALID")
        if spec.get("sourceWrite") is not False or spec.get("formalPublication") is not False:
            package_failures.append("UNSAFE_FLAGS")
        for dependency in spec.get("dependsOn", []):
            dependency_id = str(dependency.get("id")) if isinstance(dependency, dict) else str(dependency)
            if not any(str(other.get("id")) == dependency_id for other in specs):
                package_failures.append(f"DEPENDENCY_MISSING:{dependency_id}")
        for asset_name in REQUIRED_ASSETS:
            path = _asset_path(spec, asset_name)
            if path is None or not path.exists():
                package_failures.append(f"ASSET_MISSING:{asset_name}")
        ontology_asset = _asset_path(spec, "ontology")
        if ontology_asset and ontology_asset.exists():
            try:
                Graph().parse(str(ontology_asset), format="turtle")
            except Exception as exc:
                package_failures.append(f"ONTOLOGY_ASSET_INVALID:{exc}")
        context_asset = _asset_path(spec, "context")
        if context_asset and context_asset.exists():
            try:
                _load_json(context_asset)
            except Exception as exc:
                package_failures.append(f"CONTEXT_ASSET_INVALID:{exc}")
        for manifest_key in ("mappings", "rules", "events", "stateMachines", "actions", "tests"):
            manifest_asset = _asset_path(spec, manifest_key)
            if manifest_asset and manifest_asset.exists():
                try:
                    _load_json(manifest_asset)
                except Exception as exc:
                    package_failures.append(f"ASSET_MANIFEST_INVALID:{manifest_key}:{exc}")

        for kind, owner_map in (
            ("ownedClasses", class_owners),
            ("ownedObjectProperties", object_property_owners),
            ("ownedDatatypeProperties", datatype_property_owners),
        ):
            values = spec.get(kind, [])
            if not isinstance(values, list):
                package_failures.append(f"OWNERSHIP_LIST_INVALID:{kind}")
                continue
            for value in values:
                local_name = str(value)
                previous = owner_map.get(local_name)
                if previous and previous != package_id:
                    failures.append(f"PACKAGE_OWNERSHIP_OVERLAP:{kind}:{local_name}:{previous}:{package_id}")
                owner_map[local_name] = package_id

        events_asset = _asset_path(spec, "events")
        if events_asset and events_asset.exists():
            try:
                event_manifest = _load_json(events_asset)
                for event in event_manifest.get("entries", []):
                    event_name = str(event)
                    previous = event_owners.get(event_name)
                    if previous and previous != package_id:
                        failures.append(f"PACKAGE_EVENT_OWNERSHIP_OVERLAP:{event_name}:{previous}:{package_id}")
                    event_owners[event_name] = package_id
            except Exception:
                pass
        package_status.append({
            "id": package_id,
            "version": spec.get("version"),
            "type": spec.get("type"),
            "status": "PASS" if not package_failures else "FAIL",
            "failures": package_failures,
            "ownedClasses": len(spec.get("ownedClasses", [])),
            "ownedObjectProperties": len(spec.get("ownedObjectProperties", [])),
            "ownedDatatypeProperties": len(spec.get("ownedDatatypeProperties", [])),
        })
        failures.extend(f"PACKAGE_{package_id}:{item}" for item in package_failures)

    active_ontology = ontology_path or (PROJECT_ROOT / str(registry.get("canonicalOntology", "standards/ontology.ttl")))
    graph, ontology_failures = _load_active_ontology(active_ontology)
    failures.extend(ontology_failures)
    if graph is not None:
        ontology_classes = {
            str(subject).rsplit("/", 1)[-1]
            for subject in graph.subjects(RDF.type, OWL.Class)
            if str(subject).startswith(ONTOLOGY_NAMESPACE)
        }
        ontology_object_properties = {
            str(subject).rsplit("/", 1)[-1]
            for subject in graph.subjects(RDF.type, OWL.ObjectProperty)
            if str(subject).startswith(ONTOLOGY_NAMESPACE)
        }
        ontology_datatype_properties = {
            str(subject).rsplit("/", 1)[-1]
            for subject in graph.subjects(RDF.type, OWL.DatatypeProperty)
            if str(subject).startswith(ONTOLOGY_NAMESPACE)
        }
        for label, actual, owners in (
            ("class", ontology_classes, class_owners),
            ("objectProperty", ontology_object_properties, object_property_owners),
            ("datatypeProperty", ontology_datatype_properties, datatype_property_owners),
        ):
            missing = sorted(actual - set(owners))
            extra = sorted(set(owners) - actual)
            failures.extend(f"PACKAGE_{label.upper()}_UNOWNED:{name}" for name in missing)
            failures.extend(f"PACKAGE_{label.upper()}_NOT_IN_ONTOLOGY:{name}" for name in extra)

    # A composed registry is not valid merely because the ownership lists
    # match the old aggregate.  Resolve the declared package graph and run the
    # native composition gate as part of the same fail-closed contract.
    if registry.get("canonicalAssetMode") == "composed" and not failures:
        try:
            from ontology_package import OntologyPackageError, SemanticCompositionEngine, verify_native_composition

            output_dir = PROJECT_ROOT / str(registry.get("compositionOutput", "builds/canonical/v2"))
            plan = SemanticCompositionEngine().compose(
                package_ids=registry.get("defaultPackages"),
                output_dir=output_dir,
                ontology_version="enterprise-operations-ontology/v2",
            )
            composition_failures = verify_native_composition(plan)
            failures.extend(composition_failures)
            package_status.append({
                "id": "__composition__",
                "version": plan.get("ontologyVersion"),
                "type": "canonical-composition",
                "status": "PASS" if not composition_failures else "FAIL",
                "failures": composition_failures,
                "compositionHash": plan.get("compositionHash"),
            })
            # The tracked v2 assets are the promoted release snapshot.  Keep
            # them byte-for-byte aligned with the reproducible composition so
            # a package edit cannot silently leave a stale Canonical release.
            if ontology_path is None:
                active_paths = {
                    "ontology": PROJECT_ROOT / str(registry.get("canonicalOntology", "standards/v2/ontology.ttl")),
                    "shapes": PROJECT_ROOT / "standards/v2/enterprise-operations.shacl.ttl",
                    "vocabularies": PROJECT_ROOT / "standards/v2/vocabularies.ttl",
                    "context": PROJECT_ROOT / "standards/v2/context.jsonld",
                }
                for name, active_path in active_paths.items():
                    composed_path = Path(str(plan.get("artifacts", {}).get(name) or ""))
                    if not active_path.exists():
                        failures.append(f"PACKAGE_PROMOTED_ASSET_MISSING:{name}:{active_path}")
                    elif not composed_path.exists() or _sha256(active_path) != _sha256(composed_path):
                        failures.append(f"PACKAGE_PROMOTED_ASSET_DRIFT:{name}")
        except (OntologyPackageError, OSError, ValueError) as exc:
            failures.append(f"PACKAGE_COMPOSITION_FAILED:{exc}")

    return {
        "status": "PASS" if not failures else "FAIL",
        "registry": str(registry_file),
        "canonicalOntology": str(active_ontology),
        "packageCount": len(specs),
        "packages": package_status,
        "ownership": {
            "classes": len(class_owners),
            "objectProperties": len(object_property_owners),
            "datatypeProperties": len(datatype_property_owners),
            "events": len(event_owners),
        },
        "failures": sorted(set(failures)),
        "sourceWrite": False,
        "formalPublication": False,
    }


def package_summary(*, ontology_path: Path | None = None) -> dict[str, Any]:
    """Return a compact read-only summary suitable for the Semantic API."""
    result = verify_package_registry(ontology_path=ontology_path)
    return {
        "schemaVersion": "semantic-package-summary-v1",
        "status": result["status"].lower(),
        "packageCount": result["packageCount"],
        "packages": result["packages"],
        "ownership": result["ownership"],
        "canonicalOntology": result["canonicalOntology"],
        "failures": result["failures"],
        "sourceWrite": False,
        "formalPublication": False,
    }


if __name__ == "__main__":
    print(json.dumps(verify_package_registry(), ensure_ascii=False, indent=2))
