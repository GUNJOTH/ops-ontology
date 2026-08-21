"""Independent W3C-compatible processors used by the release gate.

The local replay engine remains useful for fast, explainable development
checks.  This module deliberately calls independent implementations for the
release gate so a passing internal subset cannot be mistaken for a standards
processor pass.
"""
from __future__ import annotations

from pathlib import Path

from rdflib import Graph


def run_independent_processors(
    data_graph: Graph,
    ontology_path: Path,
    shacl_path: Path,
) -> dict[str, object]:
    """Run pySHACL and OWL-RL when installed, with an explicit blocked state.

    The function never mutates the caller's graph or any source data.  A
    missing processor is a release-gate failure, not a silent fallback to the
    project's internal subset.
    """
    try:
        from pyshacl import validate as pyshacl_validate
    except ImportError as exc:
        return {
            "status": "blocked",
            "gateStatus": "fail",
            "missing": ["pyshacl"],
            "error": str(exc),
        }
    try:
        from owlrl import DeductiveClosure, OWLRL_Semantics
    except ImportError as exc:
        return {
            "status": "blocked",
            "gateStatus": "fail",
            "missing": ["owlrl"],
            "error": str(exc),
        }

    ontology_graph = Graph()
    ontology_graph.parse(str(ontology_path), format="turtle")
    shacl_graph = Graph()
    shacl_graph.parse(str(shacl_path), format="turtle")

    shacl_data = Graph()
    for triple in data_graph:
        shacl_data.add(triple)
    conforms, report_graph, report_text = pyshacl_validate(
        data_graph=shacl_data,
        shacl_graph=shacl_graph,
        ont_graph=ontology_graph,
        inference="rdfs",
        abort_on_first=False,
        allow_warnings=False,
        advanced=True,
        meta_shacl=True,
    )

    owl_graph = Graph()
    for triple in ontology_graph:
        owl_graph.add(triple)
    for triple in data_graph:
        owl_graph.add(triple)
    DeductiveClosure(
        OWLRL_Semantics,
        axiomatic_triples=False,
        datatype_axioms=False,
    ).expand(owl_graph)
    return {
        "status": "passed" if bool(conforms) else "failed",
        "gateStatus": "pass" if bool(conforms) else "fail",
        "pyshacl": {
            "status": "passed" if bool(conforms) else "failed",
            "conforms": bool(conforms),
            "validationReportTripleCount": len(report_graph),
            "report": str(report_text),
        },
        "owlrl": {
            "status": "passed",
            "closureTripleCount": len(owl_graph),
        },
    }
