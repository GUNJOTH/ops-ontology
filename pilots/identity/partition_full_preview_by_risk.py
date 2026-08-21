"""Partition full preview rows into safe whitespace and semantic-review queues."""
from __future__ import annotations

import argparse
import csv
import json
import pathlib
import re
from collections import Counter
from datetime import datetime, timezone


def no_whitespace(value: str) -> str:
    return re.sub(r"\s+", "", value or "")


def classify(row: dict[str, str]) -> str:
    return "safe_whitespace_only" if no_whitespace(row.get("original_description", "")) == no_whitespace(row.get("proposed_normalized_description", "")) else "semantic_review_required"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--preview-dir", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()
    preview_dir = pathlib.Path(args.preview_dir).resolve()
    output_dir = pathlib.Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=False)
    fields = None
    handles: dict[str, object] = {}
    writers: dict[str, csv.DictWriter] = {}
    counts: Counter[str] = Counter()
    by_schema: dict[str, Counter[str]] = {"HD_SAAS": Counter(), "XNY_SAAS": Counter()}
    try:
        for schema in ("HD_SAAS", "XNY_SAAS"):
            input_path = preview_dir / f"{schema.lower()}_full_semantic_preview.csv"
            with input_path.open("r", encoding="utf-8-sig", newline="") as handle:
                reader = csv.DictReader(handle)
                if fields is None:
                    fields = list(reader.fieldnames or []) + ["risk_class"]
                for risk_class in ("safe_whitespace_only", "semantic_review_required"):
                    output = output_dir / f"{schema.lower()}_{risk_class}.csv"
                    out = output.open("w", encoding="utf-8-sig", newline="")
                    handles[f"{schema}:{risk_class}"] = out
                    writers[f"{schema}:{risk_class}"] = csv.DictWriter(out, fieldnames=fields)
                    writers[f"{schema}:{risk_class}"].writeheader()
                for row in reader:
                    risk_class = classify(row)
                    row["risk_class"] = risk_class
                    writers[f"{schema}:{risk_class}"].writerow(row)
                    counts[risk_class] += 1
                    by_schema[schema][risk_class] += 1
    finally:
        for handle in handles.values():
            handle.close()

    report = {
        "run_id": f"source-scoped-semantic-risk-partition-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}",
        "preview_dir": str(preview_dir),
        "total_preview_rows": sum(counts.values()),
        "counts": dict(counts),
        "by_schema": {schema: dict(value) for schema, value in by_schema.items()},
        "policy": {
            "safe_whitespace_only": "candidate for deterministic rule after representative review",
            "semantic_review_required": "no automatic rewrite; model or human semantic rule discovery required",
        },
        "preview_only": True,
        "source_write": False,
        "formal_publication": False,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    (output_dir / "manifest.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
