"""Pure metadata dictionary query and shaping functions.

The service receives an already configured connection.  It never opens a
source-system connection and never mutates the metadata result layer.
"""
from __future__ import annotations

import sqlite3
from typing import Any


METADATA_EXPORT_FIELDS = (
    "semantic_id", "dictionary_version", "concept_type", "semantic_key", "canonical_name",
    "semantic_label_candidate", "description", "data_type", "length", "required", "domain_id",
    "parent_or_table", "source_schemas", "cross_schema_status", "ai_category", "ai_confidence",
    "ai_reason", "semantic_status", "evidence", "loaded_at",
)


def metadata_filters(
    concept_type: str | None,
    search: str | None,
    ai_category: str | None,
    semantic_status: str | None,
    source_schema: str | None,
) -> tuple[str, list[str]]:
    clauses = ["1=1"]
    parameters: list[str] = []
    if concept_type and concept_type != "all":
        clauses.append("concept_type = ?")
        parameters.append(concept_type)
    if ai_category and ai_category != "all":
        clauses.append("ai_category = ?")
        parameters.append(ai_category)
    if semantic_status and semantic_status != "all":
        clauses.append("semantic_status = ?")
        parameters.append(semantic_status)
    if source_schema and source_schema != "all":
        clauses.append("source_schemas LIKE ?")
        parameters.append(f"%{source_schema}%")
    if search and search.strip():
        term = f"%{search.strip()}%"
        clauses.append(
            "(semantic_id LIKE ? OR semantic_key LIKE ? OR canonical_name LIKE ? "
            "OR semantic_label_candidate LIKE ? OR description LIKE ? OR parent_or_table LIKE ?)"
        )
        parameters.extend([term] * 6)
    return " AND ".join(clauses), parameters


def metadata_row_payload(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "semanticId": row["semantic_id"],
        "dictionaryVersion": row["dictionary_version"],
        "conceptType": row["concept_type"],
        "semanticKey": row["semantic_key"],
        "canonicalName": row["canonical_name"],
        "semanticLabelCandidate": row["semantic_label_candidate"],
        "description": row["description"],
        "dataType": row["data_type"],
        "length": row["length"],
        "required": row["required"],
        "domainId": row["domain_id"],
        "parentOrTable": row["parent_or_table"],
        "sourceSchemas": row["source_schemas"],
        "crossSchemaStatus": row["cross_schema_status"],
        "aiCategory": row["ai_category"],
        "aiConfidence": row["ai_confidence"],
        "aiReason": row["ai_reason"],
        "semanticStatus": row["semantic_status"],
        "evidence": row["evidence"],
        "loadedAt": row["loaded_at"],
    }


def metadata_summary_payload(
    connection: sqlite3.Connection,
    *,
    duckdb_available: bool,
) -> dict[str, Any]:
    """Build the read-only metadata summary from the local result database."""
    run = connection.execute(
        "SELECT run_id,dictionary_version,row_count,loaded_at,source_write,formal_publication "
        "FROM metadata_semantic_run ORDER BY loaded_at DESC LIMIT 1"
    ).fetchone()
    if run is None:
        raise LookupError("元数据语义结果库缺少运行批次")
    concept_types = connection.execute(
        "SELECT concept_type, count(*) AS count FROM metadata_semantic_dictionary "
        "GROUP BY concept_type ORDER BY count DESC, concept_type"
    ).fetchall()
    ai_categories = connection.execute(
        "SELECT COALESCE(NULLIF(ai_category,''),'unclassified') AS category, count(*) AS count "
        "FROM metadata_semantic_dictionary GROUP BY category ORDER BY count DESC, category"
    ).fetchall()
    semantic_statuses = connection.execute(
        "SELECT COALESCE(NULLIF(semantic_status,''),'unclassified') AS status, count(*) AS count "
        "FROM metadata_semantic_dictionary GROUP BY status ORDER BY count DESC, status"
    ).fetchall()
    finding_count = int(connection.execute("SELECT count(*) FROM metadata_validation_findings").fetchone()[0])
    finding_types = connection.execute(
        "SELECT finding_type, count(*) AS count FROM metadata_validation_findings "
        "GROUP BY finding_type ORDER BY count DESC, finding_type"
    ).fetchall()
    return {
        "resultVersion": run["dictionary_version"],
        "runId": run["run_id"],
        "loadedAt": run["loaded_at"],
        "total": int(run["row_count"]),
        "conceptTypes": [{"value": row["concept_type"], "count": int(row["count"])} for row in concept_types],
        "aiCategories": [{"value": row["category"], "count": int(row["count"])} for row in ai_categories],
        "semanticStatuses": [{"value": row["status"], "count": int(row["count"])} for row in semantic_statuses],
        "validation": {
            "findingCount": finding_count,
            "findingTypes": [{"value": row["finding_type"], "count": int(row["count"])} for row in finding_types],
        },
        "sourceWrite": False,
        "formalPublication": False,
        "source": "local-versioned-result-layer",
        "duckdbAvailable": duckdb_available,
    }


def metadata_catalog_payload(
    connection: sqlite3.Connection,
    *,
    page: int,
    page_size: int,
    concept_type: str | None,
    search: str | None,
    ai_category: str | None,
    semantic_status: str | None,
    source_schema: str | None,
) -> dict[str, Any]:
    """Query a page from the versioned local metadata dictionary."""
    where_sql, parameters = metadata_filters(concept_type, search, ai_category, semantic_status, source_schema)
    total = int(connection.execute(
        f"SELECT count(*) FROM metadata_semantic_dictionary WHERE {where_sql}", parameters
    ).fetchone()[0])
    rows = connection.execute(
        f"SELECT * FROM metadata_semantic_dictionary WHERE {where_sql} "
        "ORDER BY concept_type, canonical_name, semantic_id LIMIT ? OFFSET ?",
        [*parameters, page_size, (page - 1) * page_size],
    ).fetchall()
    return {
        "items": [metadata_row_payload(row) for row in rows],
        "total": total,
        "page": page,
        "pageSize": page_size,
        "sourceWrite": False,
        "formalPublication": False,
    }


def metadata_catalog_detail_payload(
    connection: sqlite3.Connection,
    semantic_id: str,
) -> dict[str, Any] | None:
    """Return one metadata concept and same-name related concepts."""
    row = connection.execute(
        "SELECT * FROM metadata_semantic_dictionary WHERE semantic_id=?", (semantic_id,)
    ).fetchone()
    if row is None:
        return None
    related = connection.execute(
        "SELECT * FROM metadata_semantic_dictionary WHERE concept_type=? AND canonical_name=? "
        "AND semantic_id<>? ORDER BY semantic_key LIMIT 50",
        (row["concept_type"], row["canonical_name"], semantic_id),
    ).fetchall()
    return {
        "item": metadata_row_payload(row),
        "related": [metadata_row_payload(item) for item in related],
        "sourceWrite": False,
        "formalPublication": False,
    }


def metadata_export_rows(
    connection: sqlite3.Connection,
    *,
    concept_type: str | None,
    search: str | None,
    ai_category: str | None,
    semantic_status: str | None,
    source_schema: str | None,
) -> list[sqlite3.Row]:
    """Return export rows using the same filters as the catalog endpoint."""
    where_sql, parameters = metadata_filters(concept_type, search, ai_category, semantic_status, source_schema)
    return connection.execute(
        f"SELECT * FROM metadata_semantic_dictionary WHERE {where_sql} "
        "ORDER BY concept_type, canonical_name, semantic_id", parameters
    ).fetchall()
