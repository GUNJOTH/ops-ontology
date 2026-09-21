"""Load the single namespace contract used by canonical RDF builders."""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
NAMESPACE_FILE = ROOT.parent / "standards" / "namespace.json"
SPEC = json.loads(NAMESPACE_FILE.read_text(encoding="utf-8"))

BASE_NAMESPACE = str(SPEC["baseNamespace"])
ONTOLOGY_NAMESPACE = str(SPEC["ontologyNamespace"])
RESOURCE_NAMESPACE = str(SPEC["resourceNamespace"])
SOURCE_NAMESPACE = str(SPEC["sourceNamespace"])
GRAPH_NAMESPACE = str(SPEC["graphNamespace"])
CONTRACT_NAMESPACE = str(SPEC["contractNamespace"])

REQUIRED_KEYS = (
    "baseNamespace",
    "ontologyNamespace",
    "resourceNamespace",
    "sourceNamespace",
    "graphNamespace",
    "contractNamespace",
)


def validate_namespace_contract(spec: dict[str, object] | None = None) -> list[str]:
    """Validate the namespace file used by every semantic projection."""
    value = spec or SPEC
    failures: list[str] = []
    namespaces = {key: str(value.get(key) or "") for key in REQUIRED_KEYS}
    for key, namespace in namespaces.items():
        if not namespace.startswith("https://") or not namespace.endswith("/"):
            failures.append(f"NAMESPACE_INVALID:{key}")
    if namespaces["ontologyNamespace"] == namespaces["resourceNamespace"]:
        failures.append("NAMESPACE_ONTOLOGY_RESOURCE_MUST_DIFFER")
    rules = value.get("iriRules")
    if not isinstance(rules, dict) or any(not str(rules.get(key) or "") for key in ("class", "property", "resource", "source", "graph")):
        failures.append("IRI_RULES_INCOMPLETE")
    policy = value.get("changePolicy")
    if not isinstance(policy, dict) or policy.get("stableIri") is not True or policy.get("sourceSystemsRemainReadOnly") is not True:
        failures.append("NAMESPACE_CHANGE_POLICY_INVALID")
    return sorted(set(failures))
