"""Pure metadata dictionary shaping and filter construction."""
from __future__ import annotations

import sqlite3
from typing import Any


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

