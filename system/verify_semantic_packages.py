"""CLI entrypoint for the platform/industry package consistency gate."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from semantic_packages import verify_package_registry


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify Semantic Platform Core and industry package ownership")
    parser.add_argument("--ontology", type=Path, default=None, help="active aggregate ontology path")
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()
    result = verify_package_registry(ontology_path=args.ontology)
    payload = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
    print(payload)
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
