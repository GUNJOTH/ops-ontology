"""Cluster non-whitespace preview changes into evidence-backed rule patterns."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import pathlib
import unicodedata
from collections import defaultdict
from datetime import datetime, timezone

from safe_convert import to_int


def no_whitespace(value: str) -> str:
    return "".join(ch for ch in value or "" if not ch.isspace())


def mapping_signature(original: str, proposed: str) -> tuple[str, str]:
    """Return a stable character-level NFKC mapping signature and readable pairs."""
    pairs: set[tuple[str, str]] = set()
    for char in original:
        mapped = unicodedata.normalize("NFKC", char)
        if mapped != char and not char.isspace():
            pairs.add((f"U+{ord(char):04X}", mapped))
    normalized_original = unicodedata.normalize("NFKC", no_whitespace(original))
    normalized_proposed = no_whitespace(proposed)
    if normalized_original != normalized_proposed:
        return "contextual_or_composed_change", ""
    if not pairs:
        return "contextual_or_composed_change", ""
    readable = []
    for codepoint, mapped in sorted(pairs):
        try:
            name = unicodedata.name(chr(int(codepoint[2:], 16)), "UNKNOWN")
        except ValueError:
            name = "UNKNOWN"
        readable.append(f"{codepoint}({name})->{mapped}")
    signature = "|".join(f"{codepoint}->{mapped}" for codepoint, mapped in sorted(pairs))
    return signature, "; ".join(readable)


def cluster_id(schema: str, signature: str) -> str:
    digest = hashlib.sha256(f"{schema}|{signature}".encode("utf-8")).hexdigest()[:12]
    return f"semantic-{schema.lower()}-{digest}"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--partition-dir", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--examples-per-cluster", type=int, default=8)
    args = parser.parse_args()
    partition_dir = pathlib.Path(args.partition_dir).resolve()
    output_dir = pathlib.Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=False)

    groups: dict[str, dict[str, object]] = {}
    assignment_path = output_dir / "cluster_assignments.csv"
    assignment_fields = [
        "cluster_id", "source_schema", "site_id", "asset_number",
        "original_description", "proposed_normalized_description",
        "source_row_hash", "pattern_signature", "pattern_readable",
        "change_class",
    ]
    with assignment_path.open("w", encoding="utf-8-sig", newline="") as assignment_handle:
        assignment_writer = csv.DictWriter(assignment_handle, fieldnames=assignment_fields)
        assignment_writer.writeheader()
        for schema in ("HD_SAAS", "XNY_SAAS"):
            input_path = partition_dir / f"{schema.lower()}_semantic_review_required.csv"
            with input_path.open("r", encoding="utf-8-sig", newline="") as handle:
                for row in csv.DictReader(handle):
                    signature, readable = mapping_signature(
                        row.get("original_description", ""),
                        row.get("proposed_normalized_description", ""),
                    )
                    cid = cluster_id(schema, signature)
                    item = groups.setdefault(cid, {
                        "cluster_id": cid,
                        "source_schema": schema,
                        "pattern_signature": signature,
                        "pattern_readable": readable,
                        "count": 0,
                        "sites": set(),
                        "examples": [],
                    })
                    item["count"] = to_int(item["count"]) + 1
                    item["sites"].add(row.get("site_id", ""))
                    if len(item["examples"]) < args.examples_per_cluster:
                        item["examples"].append({
                            "site_id": row.get("site_id", ""),
                            "asset_number": row.get("asset_number", ""),
                            "before": row.get("original_description", ""),
                            "after": row.get("proposed_normalized_description", ""),
                            "source_row_hash": row.get("source_row_hash", ""),
                        })
                    assignment_writer.writerow({
                        "cluster_id": cid,
                        "source_schema": schema,
                        "site_id": row.get("site_id", ""),
                        "asset_number": row.get("asset_number", ""),
                        "original_description": row.get("original_description", ""),
                        "proposed_normalized_description": row.get("proposed_normalized_description", ""),
                        "source_row_hash": row.get("source_row_hash", ""),
                        "pattern_signature": signature,
                        "pattern_readable": readable,
                        "change_class": "semantic_review_required",
                    })

    cluster_fields = [
        "cluster_id", "source_schema", "pattern_signature", "pattern_readable",
        "count", "site_count", "examples_json", "status", "rule_draft_status",
    ]
    summary_path = output_dir / "clusters.csv"
    with summary_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=cluster_fields)
        writer.writeheader()
        for item in sorted(groups.values(), key=lambda value: (-to_int(value["count"]), str(value["cluster_id"]))):
            writer.writerow({
                "cluster_id": item["cluster_id"],
                "source_schema": item["source_schema"],
                "pattern_signature": item["pattern_signature"],
                "pattern_readable": item["pattern_readable"],
                "count": item["count"],
                "site_count": len(item["sites"]),
                "examples_json": json.dumps(item["examples"], ensure_ascii=False),
                "status": "evidence_clustered",
                "rule_draft_status": "not_generated",
            })

    by_schema = defaultdict(lambda: {"clusters": 0, "rows": 0})
    for item in groups.values():
        by_schema[item["source_schema"]]["clusters"] += 1
        by_schema[item["source_schema"]]["rows"] += to_int(item["count"])
    report = {
        "run_id": f"semantic-change-clustering-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}",
        "partition_dir": str(partition_dir),
        "cluster_count": len(groups),
        "clustered_row_count": sum(to_int(item["count"]) for item in groups.values()),
        "by_schema": dict(by_schema),
        "clusters_file": summary_path.name,
        "assignments_file": assignment_path.name,
        "policy": "cluster evidence only; no rewrite, approval, source write, or publication",
        "source_write": False,
        "formal_publication": False,
        "next_gate": "agent rule drafts, per-cluster replay, preview, and approval",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    (output_dir / "manifest.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
