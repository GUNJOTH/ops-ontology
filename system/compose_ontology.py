"""Compose native ontology packages into a Canonical asset set."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from ontology_package import OntologyPackageError, SemanticCompositionEngine, verify_native_composition


def main() -> int:
    parser = argparse.ArgumentParser(description="Compose native Semantic Ontology Packages")
    parser.add_argument("--package", dest="packages", action="append", help="package ID; repeat for a package set")
    parser.add_argument("--output", type=Path, default=Path("builds/canonical/v2"))
    parser.add_argument("--ontology-version", default="enterprise-operations-ontology/v2")
    args = parser.parse_args()
    try:
        plan = SemanticCompositionEngine().compose(
            package_ids=args.packages,
            output_dir=args.output.resolve(),
            ontology_version=args.ontology_version,
        )
        failures = verify_native_composition(plan)
        if failures:
            print(json.dumps({"status": "FAIL", "failures": failures}, ensure_ascii=False, indent=2))
            return 1
        print(json.dumps({"status": "PASS", **plan}, ensure_ascii=False, indent=2))
        return 0
    except OntologyPackageError as exc:
        print(json.dumps({"status": "FAIL", "failures": [str(exc)]}, ensure_ascii=False, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
