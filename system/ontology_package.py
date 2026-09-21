"""Native ontology package resolver and composition engine.

The resolver answers which versioned packages are installed.  The composition
engine answers what the resulting Canonical assets are.  Both are local,
read-only with respect to source systems and fail closed on semantic conflicts.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from rdflib import Graph, Literal, URIRef
from rdflib.namespace import OWL, RDF, RDFS, SKOS
from semantic_lib import content_hash, utc_now, write_json_atomic

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGES_ROOT = PROJECT_ROOT / "packages"
PACKAGE_REGISTRY = PACKAGES_ROOT / "manifest.json"
ONTOLOGY_NAMESPACE = "https://semantic.local/ontology/"
PACKAGE_NAMESPACE = "https://semantic.local/package/"
PROV_NAMESPACE = "http://www.w3.org/ns/prov#"
_SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


class OntologyPackageError(ValueError):
    """Raised when a package set cannot be resolved or composed safely."""


@dataclass(frozen=True)
class ResolvedPackage:
    package_id: str
    version: str
    package_type: str
    root: Path
    manifest: dict[str, Any]

    def asset(self, name: str) -> Path:
        value = (self.manifest.get("assets") or {}).get(name)
        if not value:
            raise OntologyPackageError(f"PACKAGE_ASSET_NOT_DECLARED:{self.package_id}:{name}")
        path = (self.root / str(value)).resolve()
        try:
            path.relative_to(self.root.resolve())
        except ValueError as exc:
            raise OntologyPackageError(f"PACKAGE_ASSET_OUTSIDE_ROOT:{self.package_id}:{name}") from exc
        if not path.exists():
            raise OntologyPackageError(f"PACKAGE_ASSET_MISSING:{self.package_id}:{name}:{path}")
        return path


def _version(value: str) -> tuple[int, int, int]:
    match = _SEMVER.fullmatch(str(value).strip())
    if not match:
        raise OntologyPackageError(f"PACKAGE_VERSION_INVALID:{value}")
    return tuple(int(part) for part in match.groups())


def _satisfies(actual: str, requirement: str | None) -> bool:
    if not requirement or requirement in {"*", "latest"}:
        return True
    if requirement.startswith("^"):
        required = _version(requirement[1:])
        current = _version(actual)
        return current[0] == required[0] and current >= required
    return _version(actual) == _version(requirement)


def _json_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise OntologyPackageError(f"PACKAGE_JSON_OBJECT_REQUIRED:{path}")
    return value


class PackageResolver:
    """Resolve a package set with exact local dependency evidence."""

    def __init__(self, registry_path: Path | None = None) -> None:
        self.registry_path = (registry_path or PACKAGE_REGISTRY).resolve()
        self.root = self.registry_path.parent
        self.registry = _json_object(self.registry_path)
        self._entries = {
            str(row["id"]): row
            for row in self.registry.get("packages", [])
            if isinstance(row, dict) and row.get("id")
        }

    def resolve(self, package_id: str, requirement: str | None = None) -> ResolvedPackage:
        entry = self._entries.get(package_id)
        if entry is None:
            raise OntologyPackageError(f"PACKAGE_NOT_REGISTERED:{package_id}")
        manifest_path = (self.root / str(entry["manifest"])).resolve()
        if not manifest_path.exists():
            raise OntologyPackageError(f"PACKAGE_MANIFEST_MISSING:{package_id}")
        manifest = _json_object(manifest_path)
        version = str(manifest.get("version") or "")
        if not _satisfies(version, requirement):
            raise OntologyPackageError(f"PACKAGE_VERSION_UNSATISFIED:{package_id}:{version}:{requirement}")
        if str(manifest.get("id")) != package_id:
            raise OntologyPackageError(f"PACKAGE_ID_MISMATCH:{package_id}")
        if str(manifest.get("namespace") or "").startswith("https://") is False:
            raise OntologyPackageError(f"PACKAGE_NAMESPACE_INVALID:{package_id}")
        if manifest.get("sourceWrite") is not False or manifest.get("formalPublication") is not False:
            raise OntologyPackageError(f"PACKAGE_UNSAFE_FLAGS:{package_id}")
        return ResolvedPackage(package_id, version, str(manifest.get("type") or ""), manifest_path.parent, manifest)

    def resolve_set(self, requested: Iterable[str] | None = None) -> list[ResolvedPackage]:
        requested_ids = list(requested or self.registry.get("defaultPackages") or [])
        resolved: dict[str, ResolvedPackage] = {}
        visiting: set[str] = set()

        def visit(package_id: str, requirement: str | None = None) -> None:
            if package_id in visiting:
                raise OntologyPackageError(f"PACKAGE_DEPENDENCY_CYCLE:{package_id}")
            package = resolved.get(package_id)
            if package is not None:
                if not _satisfies(package.version, requirement):
                    raise OntologyPackageError(f"PACKAGE_DEPENDENCY_VERSION_CONFLICT:{package_id}")
                return
            visiting.add(package_id)
            package = self.resolve(package_id, requirement)
            for dependency in package.manifest.get("dependsOn", []):
                if isinstance(dependency, dict):
                    visit(str(dependency.get("id")), str(dependency.get("version") or "*"))
                else:
                    visit(str(dependency))
            visiting.remove(package_id)
            resolved[package_id] = package

        for package_id in requested_ids:
            visit(str(package_id))
        return list(resolved.values())

    def composition_plan(self, packages: list[ResolvedPackage]) -> dict[str, Any]:
        return {
            "schemaVersion": "semantic-composition-plan-v1",
            "packages": [
                {
                    "id": package.package_id,
                    "version": package.version,
                    "type": package.package_type,
                    "namespace": package.manifest.get("namespace"),
                    "dependsOn": package.manifest.get("dependsOn", []),
                    "assets": {
                        name: {
                            "path": str(package.asset(name)),
                            "sha256": hashlib.sha256(package.asset(name).read_bytes()).hexdigest(),
                        }
                        for name in ("ontology", "shapes", "vocabularies", "context")
                    },
                }
                for package in packages
            ],
            "sourceWrite": False,
            "formalPublication": False,
        }


def _declaration_subjects(graph: Graph) -> set[URIRef]:
    subjects: set[URIRef] = set()
    for rdf_type in (OWL.Class, OWL.ObjectProperty, OWL.DatatypeProperty):
        subjects.update(subject for subject in graph.subjects(RDF.type, rdf_type) if isinstance(subject, URIRef))
    return subjects


def _merge_graphs(packages: list[ResolvedPackage], asset_name: str) -> tuple[Graph, list[str]]:
    result = Graph()
    failures: list[str] = []
    owners: dict[URIRef, str] = {}
    vocabulary_notations: dict[str, str] = {}
    for package in packages:
        graph = Graph()
        graph.parse(str(package.asset(asset_name)), format="turtle")
        for subject in _declaration_subjects(graph) if asset_name == "ontology" else set():
            previous = owners.get(subject)
            if previous and previous != package.package_id:
                failures.append(f"PACKAGE_DECLARATION_CONFLICT:{asset_name}:{subject}:{previous}:{package.package_id}")
            owners[subject] = package.package_id
        if asset_name == "shapes":
            for shape in graph.subjects(RDF.type, URIRef("http://www.w3.org/ns/shacl#NodeShape")):
                previous = owners.get(shape)
                if previous and previous != package.package_id:
                    failures.append(f"PACKAGE_SHAPE_CONFLICT:{shape}:{previous}:{package.package_id}")
                owners[shape] = package.package_id
        if asset_name == "vocabularies":
            for concept in graph.subjects(RDF.type, SKOS.Concept):
                notation = next((str(value) for value in graph.objects(concept, SKOS.notation)), None)
                label = next((str(value) for value in graph.objects(concept, SKOS.prefLabel)), "")
                if notation:
                    previous = vocabulary_notations.get(notation)
                    if previous is not None and previous != label:
                        failures.append(f"PACKAGE_VOCABULARY_CONFLICT:{notation}:{previous}:{label}")
                    vocabulary_notations[notation] = label
        for triple in graph:
            result.add(triple)
    return result, sorted(set(failures))


def _local_name(value: URIRef) -> str | None:
    text = str(value)
    if not text.startswith(ONTOLOGY_NAMESPACE):
        return None
    return text[len(ONTOLOGY_NAMESPACE):]


def _add_context_term(context: dict[str, Any], key: str, value: Any) -> None:
    previous = context.get(key)
    if previous is not None and previous != value:
        raise OntologyPackageError(f"PACKAGE_CONTEXT_CONFLICT:{key}")
    context[key] = value


def _complete_context(contexts: dict[str, Any], ontology: Graph) -> dict[str, Any]:
    """Complete the package contexts from the composed OWL declarations.

    Package context files may stay small and domain-focused.  The published
    context, however, must cover every canonical class and property so a
    consumer never receives a partially expanded semantic payload.
    """
    context = dict(contexts)
    for prefix, namespace in {
        "ex": ONTOLOGY_NAMESPACE,
        "prov": PROV_NAMESPACE,
        "skos": str(SKOS),
        "rdf": str(RDF),
        "rdfs": str(RDFS),
        "owl": str(OWL),
        "xsd": "http://www.w3.org/2001/XMLSchema#",
    }.items():
        _add_context_term(context, prefix, namespace)
    for key, value in {
        "type": "@type",
        "id": "@id",
        "wasDerivedFrom": {"@id": "prov:wasDerivedFrom", "@type": "@id"},
        "wasGeneratedBy": {"@id": "prov:wasGeneratedBy", "@type": "@id"},
        "inScheme": {"@id": "skos:inScheme", "@type": "@id"},
        "prefLabel": "skos:prefLabel",
        "altLabel": "skos:altLabel",
        "definition": "skos:definition",
        "notation": "skos:notation",
        "exactMatch": {"@id": "skos:exactMatch", "@type": "@id"},
    }.items():
        _add_context_term(context, key, value)
    for subject in ontology.subjects(RDF.type, OWL.Class):
        local = _local_name(subject)
        if local:
            _add_context_term(context, local, f"ex:{local}")
    for subject in ontology.subjects(RDF.type, OWL.ObjectProperty):
        local = _local_name(subject)
        if local:
            _add_context_term(context, local, {"@id": f"ex:{local}", "@type": "@id"})
    for subject in ontology.subjects(RDF.type, OWL.DatatypeProperty):
        local = _local_name(subject)
        if local:
            _add_context_term(context, local, f"ex:{local}")
    return context


class SemanticCompositionEngine:
    """Compose native package assets into deterministic Canonical assets."""

    def __init__(self, resolver: PackageResolver | None = None) -> None:
        self.resolver = resolver or PackageResolver()

    def compose(
        self,
        *,
        package_ids: Iterable[str] | None = None,
        output_dir: Path,
        ontology_version: str = "enterprise-operations-ontology/v2",
    ) -> dict[str, Any]:
        packages = self.resolver.resolve_set(package_ids)
        namespaces = [str(package.manifest.get("namespace") or "") for package in packages]
        if len(namespaces) != len(set(namespaces)):
            raise OntologyPackageError("PACKAGE_NAMESPACE_CONFLICT")
        ontology, ontology_failures = _merge_graphs(packages, "ontology")
        shapes, shape_failures = _merge_graphs(packages, "shapes")
        vocabularies, vocabulary_failures = _merge_graphs(packages, "vocabularies")
        failures = ontology_failures + shape_failures + vocabulary_failures
        if failures:
            raise OntologyPackageError(";".join(failures))

        canonical_iri = URIRef(f"{ONTOLOGY_NAMESPACE}{ontology_version}")
        ontology.add((canonical_iri, RDF.type, OWL.Ontology))
        ontology.add((canonical_iri, OWL.versionIRI, canonical_iri))
        ontology.add((canonical_iri, RDFS.label, Literal("Enterprise Semantic Ontology")))
        output_dir.mkdir(parents=True, exist_ok=True)
        ontology_path = output_dir / "ontology.ttl"
        shapes_path = output_dir / "enterprise-operations.shacl.ttl"
        vocabularies_path = output_dir / "vocabularies.ttl"
        ontology.serialize(destination=str(ontology_path), format="turtle")
        shapes.serialize(destination=str(shapes_path), format="turtle")
        vocabularies.serialize(destination=str(vocabularies_path), format="turtle")

        contexts: dict[str, Any] = {}
        for package in packages:
            payload = _json_object(package.asset("context"))
            context = payload.get("@context", payload)
            if isinstance(context, dict):
                for key, value in context.items():
                    if key in contexts and contexts[key] != value:
                        raise OntologyPackageError(f"PACKAGE_CONTEXT_CONFLICT:{key}")
                    contexts[key] = value
        context_path = output_dir / "context.jsonld"
        write_json_atomic(context_path, {"@context": _complete_context(contexts, ontology)})

        plan = self.resolver.composition_plan(packages)
        plan["ontologyVersion"] = ontology_version
        plan["compositionHash"] = content_hash({
            "packages": plan["packages"],
            "ontology": hashlib.sha256(ontology_path.read_bytes()).hexdigest(),
            "shapes": hashlib.sha256(shapes_path.read_bytes()).hexdigest(),
            "vocabularies": hashlib.sha256(vocabularies_path.read_bytes()).hexdigest(),
            "context": hashlib.sha256(context_path.read_bytes()).hexdigest(),
        })
        plan["builtAt"] = utc_now()
        plan["artifacts"] = {
            "ontology": str(ontology_path),
            "shapes": str(shapes_path),
            "vocabularies": str(vocabularies_path),
            "context": str(context_path),
        }
        manifest_path = output_dir / "composition-manifest.json"
        write_json_atomic(manifest_path, plan)
        plan["manifest"] = str(manifest_path)
        return plan


def verify_native_composition(plan: dict[str, Any]) -> list[str]:
    """Validate the composition result without changing any database."""
    failures: list[str] = []
    for name in ("ontology", "shapes", "vocabularies"):
        path = Path(str(plan.get("artifacts", {}).get(name) or ""))
        if not path.exists():
            failures.append(f"COMPOSITION_ARTIFACT_MISSING:{name}")
            continue
        try:
            Graph().parse(str(path), format="turtle")
        except Exception as exc:
            failures.append(f"COMPOSITION_ARTIFACT_INVALID:{name}:{exc}")
    context = Path(str(plan.get("artifacts", {}).get("context") or ""))
    if not context.exists():
        failures.append("COMPOSITION_CONTEXT_MISSING")
    return failures
