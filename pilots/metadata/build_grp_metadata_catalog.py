"""Build a candidate metadata semantic catalog from HD/XNY GRP snapshots.

This is a read-only analysis step. The output is a reviewable catalog and
cross-schema diff; it does not publish mappings or modify either source DB.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import pathlib
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timezone


CORE_TABLES = (
    "GRPOBJECT",
    "GRPATTRIBUTE",
    "GRPRELATIONSHIP",
    "GRPTABLE",
    "GRPVIEW",
    "GRPVIEWCOLUMN",
    "GRPSYSKEYS",
    "GRPSYSINDEXES",
)


def clean(value: object) -> str:
    return "" if value is None else str(value).strip()


def norm(value: object) -> str:
    text = unicodedata.normalize("NFKC", clean(value))
    return " ".join(text.split()).upper()


def key(*values: object) -> str:
    return "|".join(norm(value) for value in values)


def digest(value: object) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:20]


def read_csv(path: pathlib.Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return [{str(k): clean(v) for k, v in row.items()} for row in csv.DictReader(handle)]


def load_snapshot(manifest_path: pathlib.Path) -> tuple[dict[str, object], dict[str, list[dict[str, str]]]]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("status") != "complete" or manifest.get("source_write") is not False:
        raise SystemExit(f"Snapshot is not a complete read-only snapshot: {manifest_path}")
    tables: dict[str, list[dict[str, str]]] = {}
    for entry in manifest.get("tables", []):
        table = str(entry.get("table", "")).upper()
        if table not in CORE_TABLES:
            continue
        path = manifest_path.parent / f"{table.lower()}.csv"
        if not path.exists():
            raise SystemExit(f"Snapshot table file is missing: {path}")
        tables[table] = read_csv(path)
    missing = [table for table in CORE_TABLES if table not in tables]
    if missing:
        raise SystemExit(f"Snapshot is missing tables: {', '.join(missing)}")
    return manifest, tables


def write_csv(path: pathlib.Path, fields: list[str], rows: list[dict[str, object]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows({field: row.get(field, "") for field in fields} for row in rows)


def object_cross_key(row: dict[str, str]) -> str:
    return key(row.get("OBJECTNAME"), row.get("ENTITYNAME"))


def object_row_key(schema: str, row: dict[str, str]) -> str:
    return key(schema, row.get("OBJECTNAME"), row.get("ENTITYNAME"), row.get("ORGCODE") or row.get("ORG_CODE"), row.get("TENANTID"))


def table_role(table: str) -> str:
    return {
        "GRPOBJECT": "current_metadata.object_definition",
        "GRPATTRIBUTE": "current_metadata.attribute_definition",
        "GRPRELATIONSHIP": "current_metadata.relationship_definition",
        "GRPTABLE": "current_metadata.table_binding",
        "GRPVIEW": "current_metadata.view_definition",
        "GRPVIEWCOLUMN": "current_metadata.view_column_definition",
        "GRPSYSKEYS": "technical_metadata.index_column",
        "GRPSYSINDEXES": "technical_metadata.index_definition",
    }.get(table, "unknown")


def build_objects(schema: str, rows: list[dict[str, str]], table_rows: list[dict[str, str]]) -> tuple[list[dict[str, object]], dict[str, list[dict[str, str]]]]:
    table_names = {norm(row.get("TABLENAME")) for row in table_rows if norm(row.get("TABLENAME"))}
    objects: list[dict[str, object]] = []
    by_cross: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        cross = object_cross_key(row)
        by_cross[cross].append(row)
        entity = norm(row.get("ENTITYNAME"))
        object_name = norm(row.get("OBJECTNAME"))
        table_candidate = entity or object_name
        objects.append(
            {
                "source_schema": schema,
                "metadata_row_key": object_row_key(schema, row),
                "semantic_object_key": cross,
                "object_name": row.get("OBJECTNAME", ""),
                "entity_name": row.get("ENTITYNAME", ""),
                "description": row.get("DESCRIPTION", ""),
                "class_name": row.get("CLASSNAME", ""),
                "persistent": row.get("PERSISTENT", ""),
                "is_view": row.get("ISVIEW", ""),
                "org_code": row.get("ORGCODE") or row.get("ORG_CODE", ""),
                "tenant_id": row.get("TENANTID", ""),
                "table_candidate": table_candidate,
                "binding_status": "matched_by_entity_or_object" if table_candidate in table_names else "unmatched_needs_review",
                "mapping_status": "candidate",
                "evidence": "GRPOBJECT + GRPTABLE",
            }
        )
    return objects, by_cross


def build_attributes(schema: str, rows: list[dict[str, str]]) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for row in rows:
        label_field = "TITLE" if clean(row.get("TITLE")) else "ALIAS" if clean(row.get("ALIAS")) else "ATTRIBUTENAME" if clean(row.get("ATTRIBUTENAME")) else "COLUMNNAME"
        result.append(
            {
                "source_schema": schema,
                "metadata_row_key": key(schema, row.get("OBJECTNAME"), row.get("ENTITYNAME"), row.get("ATTRIBUTENAME"), row.get("COLUMNNAME"), row.get("TENANTID")),
                "semantic_object_key": object_cross_key(row),
                "attribute_name": row.get("ATTRIBUTENAME", ""),
                "column_name": row.get("COLUMNNAME", ""),
                "semantic_label_candidate": row.get(label_field, ""),
                "label_evidence_field": label_field,
                "data_type": row.get("TYPE", ""),
                "length": row.get("LENGTH", ""),
                "scale": row.get("SCALE", ""),
                "required": row.get("REQUIRED", ""),
                "must_be": row.get("MUSTBE", ""),
                "domain_id": row.get("DOMAINID", ""),
                "persistent": row.get("PERSISTENT", ""),
                "primary_key_sequence": row.get("PRIMARYKEYCOLSEQ", ""),
                "same_as_object": row.get("SAMEASOBJECT", ""),
                "same_as_attribute": row.get("SAMEASATTRIBUTE", ""),
                "mapping_status": "candidate",
                "evidence": "GRPATTRIBUTE",
            }
        )
    return result


def build_tables(schema: str, rows: list[dict[str, str]]) -> list[dict[str, object]]:
    return [
        {
            "source_schema": schema,
            "metadata_row_key": key(schema, row.get("TABLENAME"), row.get("ORGCODE"), row.get("TENANTID")),
            "table_name": row.get("TABLENAME", ""),
            "unique_column_name": row.get("UNIQUECOLUMNNAME", ""),
            "content_attribute": row.get("CONTENTATTRIBUTE", ""),
            "code_column_name": row.get("CODECOLUMNNAME", ""),
            "language_table": row.get("ISLANGTABLE", ""),
            "storage_type": row.get("STORAGETYPE", ""),
            "org_code": row.get("ORGCODE", ""),
            "tenant_id": row.get("TENANTID", ""),
            "mapping_status": "candidate",
            "evidence": "GRPTABLE",
        }
        for row in rows
    ]


def build_relationships(schema: str, rows: list[dict[str, str]]) -> list[dict[str, object]]:
    return [
        {
            "source_schema": schema,
            "metadata_row_key": key(schema, row.get("PARENT"), row.get("CHILD"), row.get("NAME"), row.get("TENANTID")),
            "relationship_name": row.get("NAME", ""),
            "parent_object": row.get("PARENT", ""),
            "child_object": row.get("CHILD", ""),
            "cardinality": row.get("CARDINALITY", ""),
            "db_join_required": row.get("DBJOINREQUIRED", ""),
            "where_clause": row.get("WHERECLAUSE", ""),
            "columns_config": row.get("COLUMNSCONFIG", ""),
            "tenant_id": row.get("TENANTID", ""),
            "endpoint_status": "complete" if norm(row.get("PARENT")) and norm(row.get("CHILD")) else "missing_endpoint_needs_review",
            "mapping_status": "candidate",
            "evidence": "GRPRELATIONSHIP",
        }
        for row in rows
    ]


def build_views(schema: str, rows: list[dict[str, str]]) -> list[dict[str, object]]:
    return [
        {
            "source_schema": schema,
            "metadata_row_key": key(schema, row.get("VIEWNAME"), row.get("ORGCODE"), row.get("TENANTID")),
            "view_name": row.get("VIEWNAME", ""),
            "view_select": row.get("VIEWSELECT", ""),
            "view_where": row.get("VIEWWHERE", ""),
            "view_from": row.get("VIEWFROM", ""),
            "auto_select": row.get("AUTOSELECT", ""),
            "org_code": row.get("ORGCODE", ""),
            "tenant_id": row.get("TENANTID", ""),
            "mapping_status": "candidate",
            "evidence": "GRPVIEW",
        }
        for row in rows
    ]


def build_view_columns(schema: str, rows: list[dict[str, str]]) -> list[dict[str, object]]:
    return [
        {
            "source_schema": schema,
            "metadata_row_key": key(schema, row.get("VIEWNAME"), row.get("VIEWCOLUMNNAME"), row.get("TABLENAME"), row.get("TABLECOLUMNNAME")),
            "view_name": row.get("VIEWNAME", ""),
            "view_column_name": row.get("VIEWCOLUMNNAME", ""),
            "same_storage_as": row.get("SAMESTORAGEAS", ""),
            "table_name": row.get("TABLENAME", ""),
            "table_column_name": row.get("TABLECOLUMNNAME", ""),
            "mapping_status": "candidate",
            "evidence": "GRPVIEWCOLUMN",
        }
        for row in rows
    ]


def build_indexes(schema: str, rows: list[dict[str, str]]) -> list[dict[str, object]]:
    return [
        {
            "source_schema": schema,
            "metadata_row_key": key(schema, row.get("NAME"), row.get("TBNAME"), row.get("TENANTID")),
            "index_name": row.get("NAME", ""),
            "table_name": row.get("TBNAME", ""),
            "unique_rule": row.get("UNIQUERULE", ""),
            "changed": row.get("CHANGED", ""),
            "cluster_rule": row.get("CLUSTERRULE", ""),
            "storage_partition": row.get("STORAGEPARTITION", ""),
            "required": row.get("REQUIRED", ""),
            "text_search": row.get("TEXTSEARCH", ""),
            "tenant_id": row.get("TENANTID", ""),
            "mapping_status": "candidate",
            "evidence": "GRPSYSINDEXES",
        }
        for row in rows
    ]


def build_index_columns(schema: str, rows: list[dict[str, str]]) -> list[dict[str, object]]:
    return [
        {
            "source_schema": schema,
            "metadata_row_key": key(schema, row.get("IXNAME"), row.get("COLNAME"), row.get("COLSEQ"), row.get("TENANTID")),
            "index_name": row.get("IXNAME", ""),
            "column_name": row.get("COLNAME", ""),
            "column_sequence": row.get("COLSEQ", ""),
            "ordering": row.get("ORDERING", ""),
            "changed": row.get("CHANGED", ""),
            "tenant_id": row.get("TENANTID", ""),
            "mapping_status": "candidate",
            "evidence": "GRPSYSKEYS",
        }
        for row in rows
    ]


def add_cross_schema_keys(
    attributes: list[dict[str, object]],
    tables: list[dict[str, object]],
    relationships: list[dict[str, object]],
    views: list[dict[str, object]],
    view_columns: list[dict[str, object]],
    indexes: list[dict[str, object]],
    index_columns: list[dict[str, object]],
) -> None:
    """Add comparison keys that deliberately exclude the source schema.

    ``metadata_row_key`` is a source-row identity and therefore includes the
    schema.  It must not be used to compare HD_SAAS with XNY_SAAS, otherwise
    every row is incorrectly reported as HD_ONLY/XNY_ONLY.
    """
    for row in attributes:
        row["cross_schema_key"] = key(
            row.get("semantic_object_key"),
            row.get("attribute_name"),
            row.get("column_name"),
            row.get("tenant_id"),
        )
    for row in tables:
        row["cross_schema_key"] = key(
            row.get("table_name"),
            row.get("org_code"),
            row.get("tenant_id"),
        )
    for row in relationships:
        row["cross_schema_key"] = key(
            row.get("parent_object"),
            row.get("child_object"),
            row.get("relationship_name"),
            row.get("tenant_id"),
        )
    for row in views:
        row["cross_schema_key"] = key(row.get("view_name"), row.get("org_code"), row.get("tenant_id"))
    for row in view_columns:
        row["cross_schema_key"] = key(
            row.get("view_name"),
            row.get("view_column_name"),
            row.get("table_name"),
            row.get("table_column_name"),
        )
    for row in indexes:
        row["cross_schema_key"] = key(row.get("index_name"), row.get("table_name"), row.get("tenant_id"))
    for row in index_columns:
        row["cross_schema_key"] = key(
            row.get("index_name"),
            row.get("column_name"),
            row.get("column_sequence"),
            row.get("tenant_id"),
        )


def compare_by_key(hd_rows: list[dict[str, object]], xny_rows: list[dict[str, object]], key_field: str, fields: list[str]) -> list[dict[str, object]]:
    hd = defaultdict(list)
    xny = defaultdict(list)
    for row in hd_rows:
        hd[str(row.get(key_field, ""))].append(row)
    for row in xny_rows:
        xny[str(row.get(key_field, ""))].append(row)
    output: list[dict[str, object]] = []
    for semantic_key in sorted(set(hd) | set(xny)):
        h = hd.get(semantic_key, [])
        x = xny.get(semantic_key, [])
        status = "same" if h and x else "HD_ONLY" if h else "XNY_ONLY"
        if h and x:
            differences = []
            for field in fields:
                hv = sorted({clean(item.get(field)) for item in h})
                xv = sorted({clean(item.get(field)) for item in x})
                if hv != xv:
                    differences.append(field)
            if differences:
                status = "needs_review"
            output.append(
                {
                    "semantic_key": semantic_key,
                    "status": status,
                    "hd_count": len(h),
                    "xny_count": len(x),
                    "differences": ",".join(differences),
                    "hd_evidence": digest(json.dumps(h, ensure_ascii=False, sort_keys=True)),
                    "xny_evidence": digest(json.dumps(x, ensure_ascii=False, sort_keys=True)),
                }
            )
        else:
            output.append(
                {
                    "semantic_key": semantic_key,
                    "status": status,
                    "hd_count": len(h),
                    "xny_count": len(x),
                    "differences": "missing_in_xny" if h else "missing_in_hd",
                    "hd_evidence": digest(json.dumps(h, ensure_ascii=False, sort_keys=True)) if h else "",
                    "xny_evidence": digest(json.dumps(x, ensure_ascii=False, sort_keys=True)) if x else "",
                }
            )
    return output


def quality_findings(schema: str, objects: list[dict[str, object]], attributes: list[dict[str, object]], relationships: list[dict[str, object]]) -> list[dict[str, object]]:
    findings: list[dict[str, object]] = []
    for kind, rows, fields in [
        ("duplicate_object_key", objects, ["metadata_row_key"]),
        ("duplicate_attribute_key", attributes, ["metadata_row_key"]),
        ("duplicate_relationship_key", relationships, ["metadata_row_key"]),
    ]:
        counter = Counter(tuple(str(row.get(field, "")) for field in fields) for row in rows)
        for item_key, count in counter.items():
            if count > 1:
                findings.append({"source_schema": schema, "finding_type": kind, "severity": "needs_review", "semantic_key": "|".join(item_key), "count": count, "message": "duplicate metadata key"})
    known_objects = {str(row.get("semantic_object_key", "")) for row in objects}
    for row in attributes:
        if str(row.get("semantic_object_key", "")) not in known_objects:
            findings.append({"source_schema": schema, "finding_type": "attribute_unknown_object", "severity": "needs_review", "semantic_key": row.get("metadata_row_key", ""), "count": 1, "message": "attribute parent object is absent"})
    for row in relationships:
        if not norm(row.get("parent_object")) or not norm(row.get("child_object")):
            findings.append({"source_schema": schema, "finding_type": "relationship_missing_endpoint", "severity": "needs_review", "semantic_key": row.get("metadata_row_key", ""), "count": 1, "message": "relationship endpoint is empty"})
    for row in objects:
        if row.get("binding_status") == "unmatched_needs_review":
            findings.append({"source_schema": schema, "finding_type": "object_table_unmatched", "severity": "needs_review", "semantic_key": row.get("semantic_object_key", ""), "count": 1, "message": "object has no exact GRPTABLE binding by entity or object name"})
    return findings


def main() -> None:
    parser = argparse.ArgumentParser(description="Build candidate HD/XNY GRP metadata catalog")
    parser.add_argument("--hd-manifest", required=True)
    parser.add_argument("--xny-manifest", required=True)
    parser.add_argument("--output-root", default=str(pathlib.Path(__file__).resolve().parent / "catalog"))
    args = parser.parse_args()
    hd_manifest_path = pathlib.Path(args.hd_manifest).resolve()
    xny_manifest_path = pathlib.Path(args.xny_manifest).resolve()
    hd_manifest, hd_tables = load_snapshot(hd_manifest_path)
    xny_manifest, xny_tables = load_snapshot(xny_manifest_path)
    captured_at = datetime.now(timezone.utc)
    run_id = f"grp-metadata-catalog-{captured_at.strftime('%Y%m%dT%H%M%SZ')}"
    output_dir = pathlib.Path(args.output_root) / run_id
    output_dir.mkdir(parents=True, exist_ok=False)

    hd_objects, _ = build_objects("HD_SAAS", hd_tables["GRPOBJECT"], hd_tables["GRPTABLE"])
    xny_objects, _ = build_objects("XNY_SAAS", xny_tables["GRPOBJECT"], xny_tables["GRPTABLE"])
    hd_attributes = build_attributes("HD_SAAS", hd_tables["GRPATTRIBUTE"])
    xny_attributes = build_attributes("XNY_SAAS", xny_tables["GRPATTRIBUTE"])
    hd_tables_catalog = build_tables("HD_SAAS", hd_tables["GRPTABLE"])
    xny_tables_catalog = build_tables("XNY_SAAS", xny_tables["GRPTABLE"])
    hd_relationships = build_relationships("HD_SAAS", hd_tables["GRPRELATIONSHIP"])
    xny_relationships = build_relationships("XNY_SAAS", xny_tables["GRPRELATIONSHIP"])
    hd_views = build_views("HD_SAAS", hd_tables["GRPVIEW"])
    xny_views = build_views("XNY_SAAS", xny_tables["GRPVIEW"])
    hd_view_columns = build_view_columns("HD_SAAS", hd_tables["GRPVIEWCOLUMN"])
    xny_view_columns = build_view_columns("XNY_SAAS", xny_tables["GRPVIEWCOLUMN"])
    hd_indexes = build_indexes("HD_SAAS", hd_tables["GRPSYSINDEXES"])
    xny_indexes = build_indexes("XNY_SAAS", xny_tables["GRPSYSINDEXES"])
    hd_index_columns = build_index_columns("HD_SAAS", hd_tables["GRPSYSKEYS"])
    xny_index_columns = build_index_columns("XNY_SAAS", xny_tables["GRPSYSKEYS"])
    add_cross_schema_keys(
        hd_attributes + xny_attributes,
        hd_tables_catalog + xny_tables_catalog,
        hd_relationships + xny_relationships,
        hd_views + xny_views,
        hd_view_columns + xny_view_columns,
        hd_indexes + xny_indexes,
        hd_index_columns + xny_index_columns,
    )

    object_diff = compare_by_key(hd_objects, xny_objects, "semantic_object_key", ["description", "class_name", "persistent", "is_view"])
    attribute_diff = compare_by_key(hd_attributes, xny_attributes, "cross_schema_key", ["attribute_name", "column_name", "data_type", "length", "scale", "required", "domain_id", "persistent", "primary_key_sequence"])
    relationship_diff = compare_by_key(hd_relationships, xny_relationships, "cross_schema_key", ["relationship_name", "parent_object", "child_object", "cardinality", "db_join_required", "where_clause"])
    table_diff = compare_by_key(hd_tables_catalog, xny_tables_catalog, "cross_schema_key", ["table_name", "unique_column_name", "content_attribute", "code_column_name", "storage_type"])
    view_diff = compare_by_key(hd_views, xny_views, "cross_schema_key", ["view_name", "view_where", "view_from", "auto_select"])
    view_column_diff = compare_by_key(hd_view_columns, xny_view_columns, "cross_schema_key", ["view_name", "view_column_name", "table_name", "table_column_name"])
    index_diff = compare_by_key(hd_indexes, xny_indexes, "cross_schema_key", ["index_name", "table_name", "unique_rule", "cluster_rule", "required", "text_search"])
    index_column_diff = compare_by_key(hd_index_columns, xny_index_columns, "cross_schema_key", ["index_name", "column_name", "column_sequence", "ordering"])
    findings = quality_findings("HD_SAAS", hd_objects, hd_attributes, hd_relationships) + quality_findings("XNY_SAAS", xny_objects, xny_attributes, xny_relationships)

    write_csv(output_dir / "metadata_objects.csv", list(hd_objects[0]) if hd_objects else [], hd_objects + xny_objects)
    write_csv(output_dir / "metadata_attributes.csv", list(hd_attributes[0]) if hd_attributes else [], hd_attributes + xny_attributes)
    write_csv(output_dir / "metadata_tables.csv", list(hd_tables_catalog[0]) if hd_tables_catalog else [], hd_tables_catalog + xny_tables_catalog)
    write_csv(output_dir / "metadata_relationships.csv", list(hd_relationships[0]) if hd_relationships else [], hd_relationships + xny_relationships)
    write_csv(output_dir / "metadata_views.csv", list(hd_views[0]) if hd_views else [], hd_views + xny_views)
    write_csv(output_dir / "metadata_view_columns.csv", list(hd_view_columns[0]) if hd_view_columns else [], hd_view_columns + xny_view_columns)
    write_csv(output_dir / "metadata_indexes.csv", list(hd_indexes[0]) if hd_indexes else [], hd_indexes + xny_indexes)
    write_csv(output_dir / "metadata_index_columns.csv", list(hd_index_columns[0]) if hd_index_columns else [], hd_index_columns + xny_index_columns)
    diff_fields = ["semantic_key", "status", "hd_count", "xny_count", "differences", "hd_evidence", "xny_evidence"]
    write_csv(output_dir / "object_cross_schema_diff.csv", diff_fields, object_diff)
    write_csv(output_dir / "attribute_cross_schema_diff.csv", diff_fields, attribute_diff)
    write_csv(output_dir / "relationship_cross_schema_diff.csv", diff_fields, relationship_diff)
    write_csv(output_dir / "table_cross_schema_diff.csv", diff_fields, table_diff)
    write_csv(output_dir / "view_cross_schema_diff.csv", diff_fields, view_diff)
    write_csv(output_dir / "view_column_cross_schema_diff.csv", diff_fields, view_column_diff)
    write_csv(output_dir / "index_cross_schema_diff.csv", diff_fields, index_diff)
    write_csv(output_dir / "index_column_cross_schema_diff.csv", diff_fields, index_column_diff)
    write_csv(output_dir / "metadata_quality_findings.csv", ["source_schema", "finding_type", "severity", "semantic_key", "count", "message"], findings)

    all_diffs = object_diff + attribute_diff + relationship_diff + table_diff + view_diff + view_column_diff + index_diff + index_column_diff
    status_counts = Counter(row["status"] for row in all_diffs)
    manifest = {
        "run_id": run_id,
        "generated_at": captured_at.isoformat(),
        "status": "candidate_only",
        "source_write": False,
        "formal_publication": False,
        "source_snapshots": {
            "HD_SAAS": hd_manifest.get("source_snapshot_id"),
            "XNY_SAAS": xny_manifest.get("source_snapshot_id"),
        },
        "row_counts": {
            "hd_objects": len(hd_objects),
            "xny_objects": len(xny_objects),
            "hd_attributes": len(hd_attributes),
            "xny_attributes": len(xny_attributes),
            "hd_tables": len(hd_tables_catalog),
            "xny_tables": len(xny_tables_catalog),
            "hd_relationships": len(hd_relationships),
            "xny_relationships": len(xny_relationships),
            "hd_views": len(hd_views),
            "xny_views": len(xny_views),
            "hd_view_columns": len(hd_view_columns),
            "xny_view_columns": len(xny_view_columns),
            "hd_indexes": len(hd_indexes),
            "xny_indexes": len(xny_indexes),
            "hd_index_columns": len(hd_index_columns),
            "xny_index_columns": len(xny_index_columns),
            "quality_findings": len(findings),
        },
        "cross_schema_status_counts": dict(sorted(status_counts.items())),
        "review_policy": "candidate mappings require metadata evidence and business confirmation before promotion",
        "next_gate": "review high-impact object, attribute, key and relationship differences",
    }
    (output_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"run_id": run_id, "output_dir": str(output_dir), "status": manifest["status"], "row_counts": manifest["row_counts"], "cross_schema_status_counts": manifest["cross_schema_status_counts"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
