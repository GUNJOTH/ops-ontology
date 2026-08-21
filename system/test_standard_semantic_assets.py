"""Small regression checks for the local SHACL/Core asset implementation."""
from __future__ import annotations

from rdflib import Dataset, Literal, Namespace, URIRef
from rdflib.namespace import RDF, XSD

from build_canonical_semantic_model import validate_shapes


EX = Namespace("https://semantic.local/ontology/")


def main() -> None:
    dataset = Dataset()
    graph = dataset.graph(URIRef("https://semantic.local/graph/test"))
    invalid_rule = EX["test-rule"]
    graph.add((invalid_rule, RDF.type, EX.Rule))
    graph.add((invalid_rule, EX.status, Literal("not-a-rule-status", datatype=XSD.string)))
    invalid_location_assignment = EX["test-location-assignment"]
    graph.add((invalid_location_assignment, RDF.type, EX.LocationAssignment))
    graph.add((invalid_location_assignment, EX.unexpectedProperty, Literal("unexpected")))
    issues = validate_shapes(dataset)
    codes = {str(issue["code"]) for issue in issues}
    required = {"minCount", "in", "closed"}
    missing = sorted(required - codes)
    if missing:
        raise SystemExit(f"SHACL regression check failed; missing codes: {missing}")
    print({"status": "PASS", "issueCount": len(issues), "codes": sorted(codes)})


if __name__ == "__main__":
    main()
