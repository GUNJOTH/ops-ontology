"""Analyze ASSET identity/description and ASSET.LOCATION/LOCATIONS relationships."""
from __future__ import annotations

import csv
import hashlib
import json
import pathlib
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parent
QUALITY = ROOT / "quality" / "hd_quality_candidates.csv"
LOCATIONS = ROOT / "context" / "locations.csv"
OUTPUT_DIR = ROOT / "kks_observed_patterns"
RELATIONSHIPS = OUTPUT_DIR / "high_quality_asset_location_relationships.csv"
SUMMARY = OUTPUT_DIR / "high_quality_relationship_summary.json"
CONFLICTS = OUTPUT_DIR / "location_description_conflicts.csv"


def clean(value: object) -> str:
    return "" if value is None else str(value).strip()


def norm(value: str) -> str:
    return re.sub(r"\s+", "", value or "")


def sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    location_lookup: dict[tuple[str, str], dict[str, str]] = {}
    with LOCATIONS.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            key = (clean(row.get("SITEID")), clean(row.get("LOCATION")))
            if key[0] and key[1]:
                location_lookup[key] = row

    fieldnames = [
        "SITEID", "ASSETNUM", "ASSET_DESCRIPTION", "LOCATION_CODE", "LOCATION_DESCRIPTION",
        "LOCATION_PARENT", "LOCATION_SYSTEM", "LOCATION_TYPE", "ASSET_LOCATION_DESCRIPTION_MATCH",
        "LOCATION_EVIDENCE_STATUS", "CANDIDATE_ID", "SOURCE_ROW_HASH", "CONTEXT_STATUS",
    ]
    location_descs: dict[tuple[str, str], Counter[str]] = defaultdict(Counter)
    location_assets: dict[tuple[str, str], set[str]] = defaultdict(set)
    asset_identity: set[tuple[str, str]] = set()
    asset_description_counts: Counter[str] = Counter()
    assetnum_shape_counts: Counter[str] = Counter()
    quality_rows = 0
    location_code_nonempty = 0
    location_description_nonempty = 0
    asset_description_nonempty = 0
    exact_matches = 0
    different_descriptions = 0
    missing_location_evidence = 0

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with QUALITY.open(encoding="utf-8-sig", newline="") as source, RELATIONSHIPS.open("w", encoding="utf-8-sig", newline="") as output:
        reader = csv.DictReader(source)
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()
        for row in reader:
            quality_rows += 1
            site = clean(row.get("SITEID"))
            assetnum = clean(row.get("ASSETNUM"))
            asset_desc = clean(row.get("ORIGINAL_DESCRIPTION"))
            asset_identity.add((site, assetnum))
            if asset_desc:
                asset_description_nonempty += 1
                asset_description_counts[asset_desc] += 1
            assetnum_shape_counts[re.sub(r"\d", "9", re.sub(r"[A-Za-z]", "A", assetnum))] += 1

            try:
                context = json.loads(row.get("CONTEXT_JSON") or "{}")
            except json.JSONDecodeError:
                context = {}
            location_context = context.get("location") or {}
            location_code = clean(location_context.get("LOCATION"))
            location_key = (site, location_code)
            location_meta = location_lookup.get(location_key, {})
            location_desc = clean(location_meta.get("DESCRIPTION")) or clean(row.get("LOCATION_DESCRIPTION"))
            parent = clean(row.get("LOCATION_PARENT"))
            location_system = clean(location_meta.get("C_SYSTEM"))
            location_type = clean(location_meta.get("TYPE"))
            if location_code:
                location_code_nonempty += 1
            if location_desc:
                location_description_nonempty += 1
            if location_code and location_desc:
                location_descs[location_key][location_desc] += 1
                location_assets[location_key].add(assetnum)
            if location_code and not location_desc:
                missing_location_evidence += 1
            if asset_desc and location_desc and norm(asset_desc) == norm(location_desc):
                exact_matches += 1
                match = "EXACT_NORMALIZED"
            elif asset_desc and location_desc:
                different_descriptions += 1
                match = "DIFFERENT_DESCRIPTIONS"
            else:
                match = "NOT_COMPARABLE"
            writer.writerow(
                {
                    "SITEID": site,
                    "ASSETNUM": assetnum,
                    "ASSET_DESCRIPTION": asset_desc,
                    "LOCATION_CODE": location_code,
                    "LOCATION_DESCRIPTION": location_desc,
                    "LOCATION_PARENT": parent,
                    "LOCATION_SYSTEM": location_system,
                    "LOCATION_TYPE": location_type,
                    "ASSET_LOCATION_DESCRIPTION_MATCH": match,
                    "LOCATION_EVIDENCE_STATUS": "DIRECT_LOCATIONS_ROW" if location_meta else "CONTEXT_ONLY_OR_MISSING",
                    "CANDIDATE_ID": clean(row.get("CANDIDATE_ID")),
                    "SOURCE_ROW_HASH": clean(row.get("SOURCE_ROW_HASH")),
                    "CONTEXT_STATUS": clean(row.get("CONTEXT_STATUS")),
                }
            )

    conflict_fields = ["SITEID", "LOCATION_CODE", "DESCRIPTION_VARIANT_COUNT", "DESCRIPTION_VARIANTS", "LINKED_ASSET_COUNT"]
    conflict_count = 0
    with CONFLICTS.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=conflict_fields)
        writer.writeheader()
        for (site, code), descriptions in sorted(location_descs.items()):
            if len(descriptions) <= 1:
                continue
            conflict_count += 1
            writer.writerow(
                {
                    "SITEID": site,
                    "LOCATION_CODE": code,
                    "DESCRIPTION_VARIANT_COUNT": len(descriptions),
                    "DESCRIPTION_VARIANTS": "; ".join(f"{desc} ({count})" for desc, count in descriptions.most_common()),
                    "LINKED_ASSET_COUNT": len(location_assets[(site, code)]),
                }
            )

    result = {
        "run_id": "hd-high-quality-relationships-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
        "scope": "frozen HD quality batch only",
        "quality_rows": quality_rows,
        "distinct_asset_identity": len(asset_identity),
        "location_code_nonempty_rows": location_code_nonempty,
        "location_description_nonempty_rows": location_description_nonempty,
        "asset_description_nonempty_rows": asset_description_nonempty,
        "asset_location_description_exact_normalized_rows": exact_matches,
        "asset_location_description_different_rows": different_descriptions,
        "asset_location_not_comparable_rows": quality_rows - exact_matches - different_descriptions,
        "location_code_description_conflict_count": conflict_count,
        "location_rows_with_no_description_evidence": missing_location_evidence,
        "top_assetnum_shapes": dict(assetnum_shape_counts.most_common(20)),
        "top_repeated_asset_descriptions": dict(asset_description_counts.most_common(20)),
        "quality_input_sha256": sha256(QUALITY),
        "source_write": False,
        "formal_publication": False,
        "status": "relationship_observations_only",
        "rule_version": "hd-quality-relationships-0.1.0",
        "outputs": [str(RELATIONSHIPS), str(CONFLICTS)],
    }
    SUMMARY.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
