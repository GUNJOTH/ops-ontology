# Semantic Ontology Packages

This directory is the native modular authoring boundary for the semantic
runtime. Package Turtle, SHACL, SKOS and JSON-LD assets are composed into the
Canonical RDF release; the composed release is the only runtime semantic
authority.

Package layers:

```text
platform-core/
  Object, Relation, Identity, Event, State, Fact, Rule, Decision, Action,
  Provenance and HumanReview primitives

thermal-operations/
  Device, Site, Location, Inspection, Defect, WorkOrder and related events
```

Use `python system/compose_ontology.py` to resolve and compose the default
package set. Use `python system/verify_semantic_packages.py` to run ownership,
asset, dependency and composition gates.
