"""Validate and isolate structural issues in the filtered metadata dictionary."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import pathlib
from collections import Counter, defaultdict


FORMAL_READY_CATEGORIES = {"same", "aligned", "system_configuration_difference", "source_specific"}
from datetime import datetime, timezone


def clean(value: object) -> str:
    return "" if value is None else str(value).strip()


def read_csv(path: pathlib.Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return [{str(key): clean(value) for key, value in row.items()} for row in csv.DictReader(handle)]


def write_csv(path: pathlib.Path, fields: list[str], rows: list[dict[str, object]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows({field: row.get(field, "") for field in fields} for row in rows)


def key(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:20]


def finding(kind: str, concept_type: str, semantic_key: str, message: str, source_schema: str = "") -> dict[str, str]:
    return {
        "finding_id": f"validation-{key(f'{kind}|{concept_type}|{semantic_key}|{source_schema}')} ".strip(),
        "finding_type": kind,
        "concept_type": concept_type,
        "semantic_key": semantic_key,
        "source_schema": source_schema,
        "severity": "isolate",
        "message": message,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate filtered metadata semantic dictionary")
    parser.add_argument("--dictionary-dir", required=True)
    parser.add_argument("--catalog-dir", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()
    dictionary_dir = pathlib.Path(args.dictionary_dir).resolve()
    catalog_dir = pathlib.Path(args.catalog_dir).resolve()
    output_dir = pathlib.Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=False)

    dictionary = read_csv(dictionary_dir / "metadata_semantic_dictionary.csv")
    objects = read_csv(catalog_dir / "metadata_objects.csv")
    attributes = read_csv(catalog_dir / "metadata_attributes.csv")
    relationships = read_csv(catalog_dir / "metadata_relationships.csv")
    view_columns = read_csv(catalog_dir / "metadata_view_columns.csv")
    indexes = read_csv(catalog_dir / "metadata_indexes.csv")
    index_columns = read_csv(catalog_dir / "metadata_index_columns.csv")
    findings: list[dict[str, str]] = []

    dict_by_key: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for row in dictionary:
        dict_by_key[(row.get("concept_type", ""), row.get("semantic_key", ""))].append(row)
        if not row.get("semantic_key"):
            findings.append(finding("empty_semantic_key", row.get("concept_type", ""), "", "semantic key is empty"))
        ai_category = clean(row.get("ai_category")).lower()
        if ai_category and ai_category not in FORMAL_READY_CATEGORIES:
            findings.append(finding("ai_non_formal_category_not_filtered", row.get("concept_type", ""), row.get("semantic_key", ""), f"AI category {ai_category!r} is not formal-ready"))
        semantic_status = clean(row.get("semantic_status")).lower()
        # ``semantic_status`` is derived from ``cross_schema_status`` by the AI
        # classifier and takes exactly one of: aligned / review_required /
        # source_specific. The previous isolation set listed AI *category*
        # values (needs_review / conflict / true_semantic_difference /
        # data_quality_or_missing_link), which never appear in semantic_status,
        # so that branch was dead code and only the ai_category check kept
        # conflicts out. Isolate the actual non-formal-ready value instead.
        if semantic_status == "review_required":
            findings.append(finding("semantic_conflict_not_filtered", row.get("concept_type", ""), row.get("semantic_key", ""), f"semantic status {semantic_status!r} is not formal-ready"))
    for (concept_type, semantic_key), rows in dict_by_key.items():
        if semantic_key and len(rows) > 1:
            findings.append(finding("duplicate_dictionary_key", concept_type, semantic_key, f"dictionary key occurs {len(rows)} times"))

    kept_keys = {(row.get("concept_type", ""), row.get("semantic_key", "")) for row in dictionary}
    object_keys = {clean(row.get("semantic_object_key")) for row in objects if clean(row.get("semantic_object_key"))}
    for row in objects:
        semantic_key = clean(row.get("semantic_object_key"))
        if ("object", semantic_key) not in kept_keys:
            continue
        if row.get("binding_status") == "unmatched_needs_review":
            findings.append(finding("object_table_unmatched", "object", semantic_key, "object has no exact GRPTABLE binding", row.get("source_schema", "")))

    for row in attributes:
        semantic_key = clean(row.get("cross_schema_key"))
        if ("attribute", semantic_key) not in kept_keys:
            continue
        parent = clean(row.get("semantic_object_key"))
        if not parent or parent not in object_keys:
            findings.append(finding("attribute_parent_missing", "attribute", semantic_key, "attribute parent object is absent from GRPOBJECT", row.get("source_schema", "")))

    for row in relationships:
        semantic_key = clean(row.get("cross_schema_key"))
        if ("relationship", semantic_key) not in kept_keys:
            continue
        if row.get("endpoint_status") != "complete":
            findings.append(finding("relationship_endpoint_missing", "relationship", semantic_key, "relationship parent or child endpoint is missing", row.get("source_schema", "")))

    for row in view_columns:
        semantic_key = clean(row.get("cross_schema_key"))
        if ("view_column", semantic_key) not in kept_keys:
            continue
        if not row.get("view_name") or not row.get("view_column_name"):
            findings.append(finding("view_column_identity_missing", "view_column", semantic_key, "view or view-column identity is empty", row.get("source_schema", "")))

    index_names_by_schema = {(row.get("source_schema", ""), row.get("index_name", "")) for row in indexes}
    for row in index_columns:
        semantic_key = clean(row.get("cross_schema_key"))
        if ("index_column", semantic_key) not in kept_keys:
            continue
        if (row.get("source_schema", ""), row.get("index_name", "")) not in index_names_by_schema:
            findings.append(finding("index_definition_missing", "index_column", semantic_key, "index column has no matching index definition", row.get("source_schema", "")))

    finding_keys = {(row["concept_type"], row["semantic_key"]) for row in findings if row["semantic_key"]}
    formal_ready = [row for row in dictionary if (row.get("concept_type", ""), row.get("semantic_key", "")) not in finding_keys]
    finding_path = output_dir / "metadata_validation_findings.csv"
    formal_path = output_dir / "formal_ready_metadata_semantic_dictionary.csv"
    fields = list(dictionary[0]) if dictionary else []
    write_csv(finding_path, ["finding_id", "finding_type", "concept_type", "semantic_key", "source_schema", "severity", "message"], findings)
    write_csv(formal_path, fields, formal_ready)
    manifest = {
        "run_id": output_dir.name,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": "validated_with_isolation" if findings else "validated",
        "input_dictionary": str(dictionary_dir / "metadata_semantic_dictionary.csv"),
        "input_count": len(dictionary),
        "formal_ready_count": len(formal_ready),
        "isolated_count": len(dictionary) - len(formal_ready),
        "finding_count": len(findings),
        "finding_counts": dict(Counter(row["finding_type"] for row in findings)),
        "dictionary_file": str(formal_path),
        "finding_file": str(finding_path),
        "source_write": False,
        "formal_publication": False,
        "formal_ready_definition": "Only rows without structural findings and with aligned/same/source_specific/system_configuration_difference semantics are loaded; isolated semantic conflicts never enter the formal layer.",
        "next_gate": "load only formal_ready dictionary into local SQLite/DuckDB; keep isolated findings out of formal layer",
    }
    (output_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False))


if __name__ == "__main__":
    main()
