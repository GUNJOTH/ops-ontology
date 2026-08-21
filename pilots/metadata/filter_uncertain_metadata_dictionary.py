"""Create a safer metadata dictionary with AI-uncertain entries excluded.

The original candidate catalog and source snapshots remain untouched. Only
the derived dictionary is filtered; every excluded row is written to an
auditable exclusion CSV.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import pathlib
from collections import Counter
from datetime import datetime, timezone


FORMAL_READY_CATEGORIES = {
    "same",
    "aligned",
    "system_configuration_difference",
    "source_specific",
}


def read_csv(path: pathlib.Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return [{str(key): "" if value is None else str(value).strip() for key, value in row.items()} for row in csv.DictReader(handle)]


def write_csv(path: pathlib.Path, fields: list[str], rows: list[dict[str, object]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows({field: row.get(field, "") for field in fields} for row in rows)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:20]


def main() -> None:
    parser = argparse.ArgumentParser(description="Exclude AI-uncertain metadata dictionary entries")
    parser.add_argument("--judgment-dir", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()
    judgment_dir = pathlib.Path(args.judgment_dir).resolve()
    output_dir = pathlib.Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=False)

    dictionary_path = judgment_dir / "metadata_semantic_dictionary.csv"
    cross_judgment_path = judgment_dir / "ai_cross_schema_difference_judgments.csv"
    dictionary_rows = read_csv(dictionary_path)
    cross_rows = read_csv(cross_judgment_path)
    cross_by_key = {
        (row.get("artifact_type", ""), row.get("semantic_key", "")): row
        for row in cross_rows
    }
    # Only configuration/alignment conclusions are safe for the formal
    # dictionary. True semantic differences, missing evidence, uncertain or
    # unknown AI labels remain isolated with an auditable reason.
    isolatable_categories = {"needs_review", "true_semantic_difference", "data_quality_or_missing_link"}
    kept: list[dict[str, str]] = []
    excluded: list[dict[str, str]] = []
    version = f"metadata-semantic-dictionary-filtered-{output_dir.name}"
    for row in dictionary_rows:
        concept_type = row.get("concept_type", "")
        key = (concept_type, row.get("semantic_key", ""))
        matched = cross_by_key.get(key)
        matched_category = (matched.get("category", "").strip().lower() if matched else "")
        if matched and matched_category not in FORMAL_READY_CATEGORIES:
            excluded.append(
                {
                    "exclusion_id": f"uncertain-{digest(concept_type + '|' + row.get('semantic_key', ''))}",
                    "concept_type": concept_type,
                    "semantic_key": row.get("semantic_key", ""),
                    "category": matched.get("category", "") or "needs_review",
                    "confidence": matched.get("confidence", ""),
                    "reason": matched.get("reason", ""),
                    "judgment_source": matched.get("judgment_source", ""),
                    "source_dictionary_version": row.get("dictionary_version", ""),
                }
            )
            continue
        cross_assessment = cross_by_key.get(key)
        if cross_assessment:
            row["ai_category"] = cross_assessment.get("category", "")
            row["ai_confidence"] = cross_assessment.get("confidence", "")
            row["ai_reason"] = cross_assessment.get("reason", "")
        row["dictionary_version"] = version
        kept.append(row)

    dictionary_out = output_dir / "metadata_semantic_dictionary.csv"
    exclusion_out = output_dir / "excluded_uncertain_metadata.csv"
    write_csv(dictionary_out, list(dictionary_rows[0]) if dictionary_rows else [], kept)
    write_csv(exclusion_out, list(excluded[0]) if excluded else ["exclusion_id", "concept_type", "semantic_key", "category", "confidence", "reason", "judgment_source", "source_dictionary_version"], excluded)
    source_manifest = json.loads((judgment_dir / "manifest.json").read_text(encoding="utf-8"))
    manifest = {
        "run_id": output_dir.name,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": "candidate_filtered_uncertain_excluded",
        "source_judgment_run": source_manifest.get("judgment_id"),
        "source_dictionary": str(dictionary_path),
        "filter_rule": "exclude every cross-schema judgment outside the formal-ready category allowlist",
        "input_dictionary_count": len(dictionary_rows),
        "kept_dictionary_count": len(kept),
        "excluded_uncertain_count": len(excluded),
        "excluded_by_concept_type": dict(Counter(row["concept_type"] for row in excluded)),
        "isolated_categories": sorted(isolatable_categories | {"unknown_or_unclassified"}),
        "formal_ready_categories": sorted(FORMAL_READY_CATEGORIES),
        "formal_ready_definition": "Only same/aligned/source_specific/system_configuration_difference; true semantic differences, missing evidence and unknown judgments stay isolated.",
        "enriched_system_configuration_count": sum(1 for row in kept if row.get("ai_category") == "system_configuration_difference"),
        "dictionary_file": str(dictionary_out),
        "exclusion_file": str(exclusion_out),
        "source_write": False,
        "formal_publication": False,
        "promotion_gate": "business confirmation still required before any source or formal-layer write",
    }
    (output_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False))


if __name__ == "__main__":
    main()
