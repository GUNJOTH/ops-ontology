"""Classify the full preview and build a compact representative review set."""
from __future__ import annotations

import argparse
import csv
import json
import pathlib
import re
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timezone


def no_whitespace(value: str) -> str:
    return re.sub(r"\s+", "", value or "")


def change_class(row: dict[str, str]) -> str:
    original = row.get("original_description", "")
    proposed = row.get("proposed_normalized_description", "")
    return "safe_whitespace_only" if no_whitespace(original) == no_whitespace(proposed) else "nfkc_non_whitespace_change"


def select_stratified(rows: list[dict[str, str]], target: int) -> list[dict[str, str]]:
    groups: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        groups[(row["source_schema"], row["change_class"])].append(row)
    group_keys = sorted(groups)
    if not group_keys or target <= 0:
        return []
    quota = max(1, target // len(group_keys))
    selected: list[dict[str, str]] = []
    selected_per_key: Counter[tuple[str, str]] = Counter()
    for key in group_keys:
        group = sorted(groups[key], key=lambda r: (r.get("site_id", ""), r.get("asset_number", "")))
        by_site: dict[str, list[dict[str, str]]] = defaultdict(list)
        for row in group:
            by_site[row.get("site_id", "")].append(row)
        sites = sorted(by_site)
        index = 0
        while len(selected) < target and index < quota and sites:
            for site in sites:
                if index < len(by_site[site]) and len(selected) < target and selected_per_key[key] < quota:
                    selected.append(by_site[site][index])
                    selected_per_key[key] += 1
            index += 1
    if len(selected) < target:
        seen = {(r["source_schema"], r["site_id"], r["asset_number"]) for r in selected}
        for row in sorted(rows, key=lambda r: (r["source_schema"], r["change_class"], r.get("site_id", ""), r.get("asset_number", ""))):
            key = (row["source_schema"], row["site_id"], row["asset_number"])
            if key not in seen:
                selected.append(row)
                seen.add(key)
                if len(selected) == target:
                    break
    return selected


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--preview-dir", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--review-target", type=int, default=200)
    args = parser.parse_args()
    preview_dir = pathlib.Path(args.preview_dir).resolve()
    output_dir = pathlib.Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=False)

    all_rows: list[dict[str, str]] = []
    summary: dict[str, dict[str, object]] = {}
    for schema in ("HD_SAAS", "XNY_SAAS"):
        path = preview_dir / f"{schema.lower()}_full_semantic_preview.csv"
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        counts = Counter()
        sites: dict[str, int] = Counter()
        for row in rows:
            row["change_class"] = change_class(row)
            row["review_reason"] = "anomaly" if row["change_class"] == "nfkc_non_whitespace_change" else "representative_safe_rule_sample"
            counts[row["change_class"]] += 1
            sites[row.get("site_id", "")] += 1
        all_rows.extend(rows)
        summary[schema] = {
            "preview_rows": len(rows),
            "change_class_counts": dict(counts),
            "site_count": len(sites),
        }

    selected = select_stratified(all_rows, args.review_target)
    fields = [
        "unified_device_id", "source_schema", "site_id", "asset_number",
        "original_description", "proposed_normalized_description",
        "change_type", "change_class", "review_reason", "location_code",
        "parent_asset_number", "classstructure_id", "classification_id",
        "status", "source_row_hash", "rule_version",
    ]
    review_path = output_dir / "representative_review_set.csv"
    with review_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(selected)

    report = {
        "run_id": f"source-scoped-semantic-review-set-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}",
        "preview_dir": str(preview_dir),
        "full_preview_row_count": len(all_rows),
        "review_target": args.review_target,
        "review_row_count": len(selected),
        "review_set": review_path.name,
        "review_policy": "stratified by source schema and change class, round-robin by site",
        "summary": summary,
        "preview_only": True,
        "source_write": False,
        "formal_publication": False,
        "next_gate": "review representative rows and anomalies, then full replay",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    (output_dir / "manifest.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
