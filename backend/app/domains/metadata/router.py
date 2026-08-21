"""Metadata read API kept separate from the application shell."""
from __future__ import annotations

import csv
import io

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response

from app.core.config import METADATA_DUCKDB_DB
from app.core.db import metadata_sqlite_connection

from .service import (
    METADATA_EXPORT_FIELDS,
    metadata_catalog_detail_payload,
    metadata_catalog_payload,
    metadata_export_rows,
    metadata_summary_payload,
)


def metadata_summary() -> dict[str, object]:
    connection = metadata_sqlite_connection()
    try:
        try:
            return metadata_summary_payload(connection, duckdb_available=METADATA_DUCKDB_DB.exists())
        except LookupError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
    finally:
        connection.close()


def metadata_catalog(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    concept_type: str | None = None,
    search: str | None = None,
    ai_category: str | None = None,
    semantic_status: str | None = None,
    source_schema: str | None = None,
) -> dict[str, object]:
    connection = metadata_sqlite_connection()
    try:
        return metadata_catalog_payload(
            connection,
            page=page,
            page_size=page_size,
            concept_type=concept_type,
            search=search,
            ai_category=ai_category,
            semantic_status=semantic_status,
            source_schema=source_schema,
        )
    finally:
        connection.close()


def metadata_catalog_detail(semantic_id: str) -> dict[str, object]:
    connection = metadata_sqlite_connection()
    try:
        payload = metadata_catalog_detail_payload(connection, semantic_id)
        if payload is None:
            raise HTTPException(status_code=404, detail="元数据语义记录不存在")
        return payload
    finally:
        connection.close()


def metadata_export(
    concept_type: str | None = None,
    search: str | None = None,
    ai_category: str | None = None,
    semantic_status: str | None = None,
    source_schema: str | None = None,
) -> Response:
    connection = metadata_sqlite_connection()
    try:
        rows = metadata_export_rows(
            connection,
            concept_type=concept_type,
            search=search,
            ai_category=ai_category,
            semantic_status=semantic_status,
            source_schema=source_schema,
        )
    finally:
        connection.close()
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(METADATA_EXPORT_FIELDS)
    for row in rows:
        writer.writerow([row[field] for field in METADATA_EXPORT_FIELDS])
    return Response(
        content=output.getvalue().encode("utf-8-sig"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=metadata_semantic_dictionary.csv"},
    )


def build_router() -> APIRouter:
    router = APIRouter()
    router.add_api_route("/api/metadata/summary", metadata_summary, methods=["GET"])
    router.add_api_route("/api/metadata/catalog", metadata_catalog, methods=["GET"])
    router.add_api_route("/api/metadata/catalog/{semantic_id}", metadata_catalog_detail, methods=["GET"])
    router.add_api_route("/api/metadata/export", metadata_export, methods=["GET"])
    return router
