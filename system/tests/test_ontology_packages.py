"""Regression tests for native Ontology Package resolution and composition."""
from __future__ import annotations

import json
import shutil
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from ontology_package import PackageResolver, SemanticCompositionEngine
from rdflib import Graph
from rdflib.namespace import OWL, RDF

ROOT = Path(__file__).resolve().parents[2]


@contextmanager
def composition_output() -> Iterator[Path]:
    """Use a repo-local disposable directory on restricted Windows hosts."""
    parent = ROOT / "system" / ".pytest-tmp"
    parent.mkdir(parents=True, exist_ok=True)
    output = parent / f"ontology-package-{uuid.uuid4().hex}"
    try:
        yield output
    finally:
        shutil.rmtree(output, ignore_errors=True)


def test_default_package_resolution_is_dependency_ordered() -> None:
    packages = PackageResolver().resolve_set()
    assert [package.package_id for package in packages] == ["platform-core", "thermal-operations"]
    assert packages[1].manifest["dependsOn"] == ["platform-core"]


def test_composition_builds_a_complete_canonical_asset_set() -> None:
    with composition_output() as output:
        plan = SemanticCompositionEngine().compose(output_dir=output)
        assert plan["sourceWrite"] is False
        assert plan["formalPublication"] is False
        assert plan["packages"][0]["id"] == "platform-core"

        ontology = Graph()
        ontology.parse(data=Path(plan["artifacts"]["ontology"]).read_text(encoding="utf-8"), format="turtle")
        assert (None, RDF.type, OWL.Class) in ontology
        assert (None, RDF.type, OWL.ObjectProperty) in ontology
        assert (None, RDF.type, OWL.DatatypeProperty) in ontology

        context = json.loads(Path(plan["artifacts"]["context"]).read_text(encoding="utf-8"))
        assert context["@context"]["Device"] == "ex:Device"
        assert context["@context"]["locatedAt"]["@type"] == "@id"
        assert context["@context"]["sourceRecordId"] == "ex:sourceRecordId"


def test_composition_manifest_records_native_asset_hashes() -> None:
    with composition_output() as output:
        plan = SemanticCompositionEngine().compose(output_dir=output)
        manifest = json.loads(Path(plan["manifest"]).read_text(encoding="utf-8"))
        assert manifest["compositionHash"]
        assert {item["id"] for item in manifest["packages"]} == {"platform-core", "thermal-operations"}
        assert set(manifest["artifacts"]) == {"ontology", "shapes", "vocabularies", "context"}
