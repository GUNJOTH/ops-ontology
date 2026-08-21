"""Attach source-grounded context to active HD candidates and route context warnings."""
from __future__ import annotations

import csv
import hashlib
import json
import pathlib
from collections import defaultdict
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parent
CONTEXT_DIR = ROOT / "context"
ACTIVE_CANDIDATES = ROOT / "candidates" / "equipment_description_candidates.csv"
OUTPUT_DIR = ROOT / "context_candidates"
OUTPUT = OUTPUT_DIR / "equipment_description_context_candidates.csv"
REVIEW = OUTPUT_DIR / "context_review_queue.csv"
MANIFEST = CONTEXT_DIR / "candidate_context_manifest.json"


def read_rows(name: str) -> list[dict[str, str]]:
    with (CONTEXT_DIR / name).open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def clean(value: object) -> str:
    return "" if value is None else str(value).strip()


def context_hash(value: dict[str, object]) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    active_rows = list(csv.DictReader((ACTIVE_CANDIDATES).open(encoding="utf-8-sig", newline="")))
    active_keys = {(clean(row.get("SITEID")), clean(row.get("ASSETNUM"))) for row in active_rows}
    referenced_locations = {(clean(row.get("SITEID")), clean(row.get("LOCATION"))) for row in active_rows if clean(row.get("LOCATION"))}

    locations = {(clean(row.get("SITEID")), clean(row.get("LOCATION"))): row for row in read_rows("locations.csv")}
    # The hierarchy table is large; retain only locations referenced by active devices.
    hierarchies: dict[tuple[str, str], dict[str, str]] = {}
    with (CONTEXT_DIR / "lochierarchy.csv").open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            key = (clean(row.get("SITEID")), clean(row.get("LOCATION")))
            if key in referenced_locations:
                hierarchies[key] = row
    classstructures = {clean(row.get("CLASSSTRUCTUREID")): row for row in read_rows("classstructure.csv")}
    classifications = {clean(row.get("CLASSIFICATIONID")): row for row in read_rows("classification.csv")}

    specs: defaultdict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for row in read_rows("assetspec.csv"):
        key = (clean(row.get("SITEID")), clean(row.get("ASSETNUM")))
        if key in active_keys:
            specs[key].append(row)
    features: defaultdict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for row in read_rows("assetfeature.csv"):
        key = (clean(row.get("SITEID")), clean(row.get("ASSETNUM")))
        if key in active_keys:
            features[key].append(row)
    feature_specs: defaultdict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for row in read_rows("assetfeaturespec.csv"):
        key = (clean(row.get("SITEID")), clean(row.get("ASSETNUM")))
        if key in active_keys:
            feature_specs[key].append(row)
    meters: defaultdict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for row in read_rows("assetmeter.csv"):
        key = (clean(row.get("SITEID")), clean(row.get("ASSETNUM")))
        if key in active_keys:
            meters[key].append(row)
    asset_hierarchy: defaultdict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for row in read_rows("assethierarchy.csv"):
        key = (clean(row.get("SITEID")), clean(row.get("ASSETNUM")))
        if key in active_keys:
            asset_hierarchy[key].append(row)
    relations: defaultdict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for row in read_rows("assetlocrelation.csv"):
        site = clean(row.get("SITEID"))
        for field in ("SOURCEASSETNUM", "TARGETASSETNUM"):
            key = (site, clean(row.get(field)))
            if key in active_keys:
                relations[key].append(row)

    fields = [
        "CANDIDATE_ID", "SOURCE_SNAPSHOT_ID", "SOURCE_ROW_HASH", "ASSETID", "SITEID", "ASSETNUM",
        "ORIGINAL_DESCRIPTION", "CANDIDATE_DESCRIPTION", "RESULT_STATUS", "RULE_VERSION", "VALIDATOR_VERSION",
        "CONTEXT_STATUS", "CONTEXT_REASON_CODES", "LOCATION_DESCRIPTION", "LOCATION_STATUS", "LOCATION_PARENT",
        "CLASSSTRUCTURE_DESCRIPTION", "CLASSIFICATION_DESCRIPTION", "SPEC_COUNT", "FEATURE_COUNT",
        "FEATURE_SPEC_COUNT", "METER_COUNT", "PARENT_ASSET_COUNT", "RELATION_COUNT", "CONTEXT_JSON", "CONTEXT_HASH",
    ]
    counts = defaultdict(int)
    reason_counts = defaultdict(int)
    with OUTPUT.open("w", encoding="utf-8-sig", newline="") as output_handle, REVIEW.open("w", encoding="utf-8-sig", newline="") as review_handle:
        writer = csv.DictWriter(output_handle, fieldnames=fields)
        review_writer = csv.DictWriter(review_handle, fieldnames=fields)
        writer.writeheader()
        review_writer.writeheader()
        for row in active_rows:
            key = (clean(row.get("SITEID")), clean(row.get("ASSETNUM")))
            location_key = (key[0], clean(row.get("LOCATION")))
            location = locations.get(location_key, {}) if location_key[1] else {}
            hierarchy = hierarchies.get(location_key, {}) if location_key[1] else {}
            classstructure = classstructures.get(clean(row.get("CLASSSTRUCTUREID")), {})
            classification = classifications.get(clean(classstructure.get("CLASSIFICATIONID")), {}) if classstructure else {}
            reasons: list[str] = []
            if location_key[1] and location_key not in locations:
                reasons.append("MISSING_LOCATION")
            if location_key[1] and location_key not in hierarchies:
                reasons.append("MISSING_LOCATION_HIERARCHY")
            if clean(row.get("CLASSSTRUCTUREID")) and not classstructure:
                reasons.append("MISSING_CLASSSTRUCTURE")
            if classstructure.get("CLASSIFICATIONID") and classstructure.get("CLASSIFICATIONID") not in classifications:
                reasons.append("MISSING_CLASSIFICATION")
            asset_classstructure = clean(row.get("CLASSSTRUCTUREID"))
            location_classstructure = clean(location.get("CLASSSTRUCTUREID"))
            if asset_classstructure and location_classstructure and asset_classstructure != location_classstructure:
                reasons.append("ASSET_LOCATION_CLASS_MISMATCH")
            if any(clean(spec.get("CLASSSTRUCTUREID")) and clean(spec.get("CLASSSTRUCTUREID")) != asset_classstructure for spec in specs[key]):
                reasons.append("SPEC_CLASSSTRUCTURE_MISMATCH")
            context = {
                "location": {
                    "LOCATION": location.get("LOCATION", ""), "DESCRIPTION": location.get("DESCRIPTION", ""),
                    "TYPE": location.get("TYPE", ""), "STATUS": location.get("STATUS", ""),
                    "CLASSSTRUCTUREID": location.get("CLASSSTRUCTUREID", ""), "SITEID": location.get("SITEID", ""),
                },
                "location_hierarchy": {
                    "LOCATION": hierarchy.get("LOCATION", ""), "PARENT": hierarchy.get("PARENT", ""),
                    "SITEID": hierarchy.get("SITEID", ""), "ORGID": hierarchy.get("ORGID", ""),
                },
                "class_structure": {
                    "CLASSSTRUCTUREID": classstructure.get("CLASSSTRUCTUREID", ""),
                    "DESCRIPTION": classstructure.get("DESCRIPTION", ""),
                    "PARENT": classstructure.get("PARENT", ""),
                    "CLASSIFICATIONID": classstructure.get("CLASSIFICATIONID", ""),
                },
                "classification": {
                    "CLASSIFICATIONID": classification.get("CLASSIFICATIONID", ""),
                    "DESCRIPTION": classification.get("DESCRIPTION", ""),
                },
                "specifications": [
                    {field: spec.get(field, "") for field in ["ASSETATTRID", "ALNVALUE", "NUMVALUE", "TABLEVALUE", "MEASUREUNITID", "ES01", "ES02", "ES03", "ES04", "ES05", "SECTION"]}
                    for spec in specs[key]
                ],
                "features": [{field: feature.get(field, "") for field in ["FEATURE", "LABEL", "CLASSSTRUCTUREID"]} for feature in features[key]],
                "feature_specifications": [{field: value.get(field, "") for field in ["ASSETATTRID", "FEATURE", "ALNVALUE", "NUMVALUE", "TABLEVALUE", "CLASSSTRUCTUREID"]} for value in feature_specs[key]],
                "meters": [{field: meter.get(field, "") for field in ["METERNAME", "ACTIVE", "MEASUREUNITID", "LASTREADING"]} for meter in meters[key]],
                "asset_hierarchy": [{field: value.get(field, "") for field in ["PARENT", "LOCATION", "WONUM"]} for value in asset_hierarchy[key]],
                "location_relations": [{field: value.get(field, "") for field in ["SOURCEASSETNUM", "SOURCELOCATION", "TARGETASSETNUM", "TARGETLOCATION", "ASSETRELATIONNUM"]} for value in relations[key]],
            }
            context_json = json.dumps(context, ensure_ascii=False, separators=(",", ":"))
            result = {field: "" for field in fields}
            for field in ["CANDIDATE_ID", "SOURCE_SNAPSHOT_ID", "SOURCE_ROW_HASH", "ASSETID", "SITEID", "ASSETNUM", "ORIGINAL_DESCRIPTION", "CANDIDATE_DESCRIPTION", "RESULT_STATUS", "RULE_VERSION", "VALIDATOR_VERSION"]:
                result[field] = row.get(field, "")
            result.update({
                "CONTEXT_STATUS": "needs_review" if reasons else "complete",
                "CONTEXT_REASON_CODES": ",".join(reasons),
                "LOCATION_DESCRIPTION": location.get("DESCRIPTION", ""),
                "LOCATION_STATUS": location.get("STATUS", ""),
                "LOCATION_PARENT": hierarchy.get("PARENT", ""),
                "CLASSSTRUCTURE_DESCRIPTION": classstructure.get("DESCRIPTION", ""),
                "CLASSIFICATION_DESCRIPTION": classification.get("DESCRIPTION", ""),
                "SPEC_COUNT": str(len(specs[key])),
                "FEATURE_COUNT": str(len(features[key])),
                "FEATURE_SPEC_COUNT": str(len(feature_specs[key])),
                "METER_COUNT": str(len(meters[key])),
                "PARENT_ASSET_COUNT": str(len(asset_hierarchy[key])),
                "RELATION_COUNT": str(len(relations[key])),
                "CONTEXT_JSON": context_json,
                "CONTEXT_HASH": context_hash(context),
            })
            writer.writerow(result)
            counts[result["CONTEXT_STATUS"]] += 1
            for reason in reasons:
                reason_counts[reason] += 1
            if reasons:
                review_writer.writerow(result)

    context_manifest = json.loads((CONTEXT_DIR / "manifest.json").read_text(encoding="utf-8"))
    # Rows carry SOURCE_SNAPSHOT_ID forward from the active ASSET candidates.
    # The manifest must therefore point at that same asset snapshot, while the
    # distinct context capture point is recorded separately. Reading
    # context/manifest.json's ``source_snapshot_id`` (the context snapshot)
    # here left the manifest inconsistent with the row-level asset snapshot id.
    asset_source_snapshot_id = clean(
        context_manifest.get("parent_asset_source_snapshot_id") or context_manifest.get("source_snapshot_id")
    )
    manifest = {
        "context_candidate_run_id": "hd-context-candidates-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
        "source_snapshot_id": asset_source_snapshot_id,
        "context_snapshot_id": clean(context_manifest.get("source_snapshot_id")),
        "input_active_candidates": len(active_rows),
        "context_candidates": dict(counts),
        "context_reason_counts": dict(reason_counts),
        "output": str(OUTPUT),
        "review_queue": str(REVIEW),
        "formal_publication": False,
    }
    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(manifest)


if __name__ == "__main__":
    main()
