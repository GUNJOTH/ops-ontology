"""Build a full read-only preview for safe source-scoped description normalization.

This scans the frozen unified-device result layer, not the DM8 source tables.
Only active, non-placeholder, non-test descriptions are eligible.  The output
contains every changed row; unchanged rows are counted but are not duplicated
into the preview CSV.  No source or formal publication layer is written.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import pathlib
import re
import sqlite3
import unicodedata
from collections import Counter
from datetime import datetime, timezone


INACTIVE = {
    "\u505c\u7528", "\u62a5\u5e9f", "\u6ce8\u9500", "\u505c\u6b62\u4f7f\u7528",
    "\u4f5c\u5e9f", "\u975e\u6d3b\u52a8", "\u4e0d\u6d3b\u52a8", "\u4e2d\u65ad",
    "\u505c\u6b62", "\u5e9f\u6b62", "\u9000\u5f79",
}
PLACEHOLDERS = {"", "string", "none", "null", "\u8bbe\u5907", "\u672a\u77e5"}


def clean(value: object) -> str:
    return "" if value is None else str(value).strip()


def normalize_description(value: object) -> str:
    text = unicodedata.normalize("NFKC", clean(value))
    return re.sub(r"\s+", " ", text).strip()


def source_row_hash(schema: str, site: str, asset: str, description: str) -> str:
    raw = "|".join((schema, site, asset, description))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description="Build full source-scoped semantic preview")
    parser.add_argument("--result-root", required=True)
    parser.add_argument("--output-dir", default="source_scoped_semantic_full_preview_v1")
    parser.add_argument("--chunk-size", type=int, default=10000)
    args = parser.parse_args()

    root = pathlib.Path(args.result_root).resolve()
    output_dir = root / args.output_dir
    output_dir.mkdir(parents=True, exist_ok=False)
    db_path = root / "identity_semantics.sqlite3"
    connection = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row

    inactive_placeholders = ",".join("?" for _ in INACTIVE)
    query = f"""
        SELECT unified_device_id, master_source_schema, site_id, asset_number,
               source_asset_id, canonical_name, location_code,
               parent_asset_number, org_id, classstructure_id,
               classification_id, status
          FROM unified_device
         WHERE master_source_schema IN ('HD_SAAS', 'XNY_SAAS')
           AND COALESCE(TRIM(canonical_name), '') <> ''
           AND LOWER(TRIM(canonical_name)) NOT IN ('string', 'none', 'null')
           AND TRIM(canonical_name) NOT IN ('\u8bbe\u5907', '\u672a\u77e5')
           AND COALESCE(canonical_name, '') NOT LIKE '%\u6d4b\u8bd5%'
           AND LOWER(COALESCE(canonical_name, '')) NOT LIKE '%test%'
           AND LOWER(COALESCE(canonical_name, '')) NOT LIKE '%xxx%'
           AND COALESCE(status, '') NOT IN ({inactive_placeholders})
         ORDER BY master_source_schema, site_id, unified_device_id
    """
    cursor = connection.execute(query, tuple(sorted(INACTIVE)))
    fields = [
        "unified_device_id", "source_schema", "site_id", "asset_number",
        "source_asset_id", "original_description",
        "proposed_normalized_description", "change_type", "location_code",
        "parent_asset_number", "org_id", "classstructure_id",
        "classification_id", "status", "source_row_hash", "rule_version",
    ]
    handles: dict[str, object] = {}
    writers: dict[str, csv.DictWriter] = {}
    counts: dict[str, Counter[str]] = {
        "HD_SAAS": Counter(), "XNY_SAAS": Counter()
    }
    try:
        for schema in ("HD_SAAS", "XNY_SAAS"):
            output = output_dir / f"{schema.lower()}_full_semantic_preview.csv"
            handle = output.open("w", encoding="utf-8-sig", newline="")
            handles[schema] = handle
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writers[schema] = writer

        while True:
            rows = cursor.fetchmany(args.chunk_size)
            if not rows:
                break
            for row in rows:
                schema = clean(row["master_source_schema"])
                original = clean(row["canonical_name"])
                proposed = normalize_description(original)
                counts[schema]["eligible_count"] += 1
                if proposed == original:
                    counts[schema]["unchanged_count"] += 1
                    continue
                counts[schema]["impact_count"] += 1
                writers[schema].writerow({
                    "unified_device_id": clean(row["unified_device_id"]),
                    "source_schema": schema,
                    "site_id": clean(row["site_id"]),
                    "asset_number": clean(row["asset_number"]),
                    "source_asset_id": clean(row["source_asset_id"]),
                    "original_description": original,
                    "proposed_normalized_description": proposed,
                    "change_type": "safe_whitespace_nfkc_preview",
                    "location_code": clean(row["location_code"]),
                    "parent_asset_number": clean(row["parent_asset_number"]),
                    "org_id": clean(row["org_id"]),
                    "classstructure_id": clean(row["classstructure_id"]),
                    "classification_id": clean(row["classification_id"]),
                    "status": clean(row["status"]),
                    "source_row_hash": source_row_hash(
                        schema, clean(row["site_id"]), clean(row["asset_number"]), original
                    ),
                    "rule_version": "source-scoped-semantic-preview-v1",
                })
    finally:
        for handle in handles.values():
            handle.close()
        connection.close()

    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    summary = {}
    for schema in ("HD_SAAS", "XNY_SAAS"):
        preview_file = output_dir / f"{schema.lower()}_full_semantic_preview.csv"
        summary[schema] = {
            **dict(counts[schema]),
            "preview_file": preview_file.name,
            "preview_sha256": sha256_file(preview_file),
        }
    result = {
        "run_id": f"source-scoped-semantic-full-preview-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}",
        "baseline_id": manifest.get("baseline_id"),
        "result_root": str(root),
        "rule_version": "source-scoped-semantic-preview-v1",
        "scope": ["HD_SAAS", "XNY_SAAS"],
        "identity_key": ["source_schema", "SITEID", "ASSETNUM"],
        "summary": summary,
        "quality_filter": {
            "active_status_only": True,
            "exclude_blank_placeholder_test_descriptions": True,
            "excluded_statuses": sorted(INACTIVE),
        },
        "preview_only": True,
        "source_write": False,
        "formal_publication": False,
        "next_gate": "representative and anomaly review, then full replay",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    (output_dir / "manifest.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
