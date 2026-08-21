"""Build source-scoped, read-only semantic samples for HD and XNY.

The output is a preview/evidence layer only.  It applies safe Unicode and
whitespace normalization to show a candidate; it never writes a device name
back to the source or formal result layer.
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


INACTIVE = {"停用", "报废", "注销", "停止使用", "作废", "非活动", "不活动", "中断", "停止", "废止", "退役"}
PLACEHOLDERS = {"", "string", "none", "null", "设备", "未知"}
KKS_LIKE = re.compile(r"^[0-9]{2}[A-Za-z0-9]+$")


def clean(value: object) -> str:
    return "" if value is None else str(value).strip()


def normalize_description(value: object) -> str:
    text = unicodedata.normalize("NFKC", clean(value))
    return re.sub(r"\s+", " ", text).strip()


def source_row_hash(schema: str, site: str, asset: str, description: str) -> str:
    raw = "|".join((schema, site, asset, description))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def fetch_sample(connection: sqlite3.Connection, schema: str, target: int) -> list[dict[str, str]]:
    eligible = """
        master_source_schema=?
        AND COALESCE(TRIM(canonical_name),'')<>''
        AND LOWER(TRIM(canonical_name)) NOT IN ('string','none','null')
        AND TRIM(canonical_name) NOT IN ('设备','未知')
        AND COALESCE(canonical_name,'') NOT LIKE '%测试%'
        AND LOWER(COALESCE(canonical_name,'')) NOT LIKE '%test%'
        AND LOWER(COALESCE(canonical_name,'')) NOT LIKE '%xxx%'
        AND COALESCE(status,'') NOT IN ({inactive})
    """.format(inactive=",".join("?" for _ in INACTIVE))
    params = [schema, *sorted(INACTIVE)]
    representatives = connection.execute(
        f"""
        WITH eligible AS (
            SELECT u.unified_device_id, u.master_source_schema, u.site_id,
                   u.asset_number, u.source_asset_id, u.canonical_name,
                   u.location_code, u.parent_asset_number, u.org_id,
                   u.classstructure_id, u.classification_id, u.status,
                   ROW_NUMBER() OVER (
                       PARTITION BY u.site_id
                       ORDER BY COALESCE(u.classstructure_id,''), u.unified_device_id
                   ) AS site_rank
              FROM unified_device u
             WHERE {eligible}
        )
        SELECT unified_device_id, master_source_schema, site_id, asset_number,
               source_asset_id, canonical_name, location_code, parent_asset_number,
               org_id, classstructure_id, classification_id, status
          FROM eligible
         WHERE site_rank=1
         ORDER BY site_id, classstructure_id, unified_device_id
         LIMIT ?
        """,
        (*params, target),
    ).fetchall()
    selected = {row[0] for row in representatives}
    if len(representatives) < target:
        fill = connection.execute(
            f"""
            SELECT u.unified_device_id, u.master_source_schema, u.site_id,
                   u.asset_number, u.source_asset_id, u.canonical_name,
                   u.location_code, u.parent_asset_number, u.org_id,
                   u.classstructure_id, u.classification_id, u.status
              FROM unified_device u
             WHERE {eligible}
             ORDER BY u.site_id, COALESCE(u.classstructure_id,''), u.unified_device_id
             LIMIT ?
            """,
            (*params, target * 30),
        ).fetchall()
        representatives.extend(row for row in fill if row[0] not in selected)
    representatives = representatives[:target]

    result: list[dict[str, str]] = []
    for row in representatives:
        (
            device_id, source_schema, site, asset_number, source_asset_id,
            original, location, parent_asset, org_id, classstructure_id,
            classification_id, status,
        ) = map(clean, row)
        normalized = normalize_description(original)
        location_description, location_parent = connection.execute(
            """
            SELECT COALESCE(f.description,''), COALESCE(f.parent_location,'')
              FROM function_location f
             WHERE f.source_schema=? AND f.site_id=? AND f.location_code=?
             ORDER BY f.location_record_id
             LIMIT 1
            """,
            (source_schema, site, location),
        ).fetchone() or ("", "")
        class_description = connection.execute(
            """
            SELECT COALESCE(description,'') FROM device_classification
             WHERE source_schema=? AND site_id=? AND classstructure_id=?
             ORDER BY classification_record_id LIMIT 1
            """,
            (source_schema, site, classstructure_id),
        ).fetchone()
        class_description = clean(class_description[0]) if class_description else ""
        kks_like = bool(KKS_LIKE.fullmatch(location))
        change_type = "unchanged" if normalized == original else "safe_whitespace_nfkc_preview"
        result.append({
            "unified_device_id": device_id,
            "source_schema": source_schema,
            "site_id": site,
            "asset_number": asset_number,
            "source_asset_id": source_asset_id,
            "original_description": original,
            "proposed_normalized_description": normalized,
            "change_type": change_type,
            "location_code": location,
            "location_description": location_description,
            "parent_location": location_parent or parent_asset,
            "parent_asset_number": parent_asset,
            "classstructure_id": classstructure_id,
            "classification_id": classification_id,
            "classification_description": class_description,
            "status": status,
            "kks_like_candidate": location if kks_like else "",
            "kks_confirmation_status": "unconfirmed_location_pattern" if kks_like else "not_detected",
            "source_row_hash": source_row_hash(source_schema, site, asset_number, original),
            "rule_version": "source-scoped-semantic-preview-v1",
        })
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Build HD/XNY source-scoped semantic samples")
    parser.add_argument("--result-root", required=True)
    parser.add_argument("--target-per-source", type=int, default=300)
    parser.add_argument("--output-dir", default="source_scoped_semantic_samples_v2")
    args = parser.parse_args()
    root = pathlib.Path(args.result_root).resolve()
    output_dir = root / args.output_dir
    output_dir.mkdir(parents=True, exist_ok=False)
    connection = sqlite3.connect(root / "identity_semantics.sqlite3")
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    summary: dict[str, object] = {}
    for schema in ("HD_SAAS", "XNY_SAAS"):
        rows = fetch_sample(connection, schema, args.target_per_source)
        output = output_dir / f"{schema.lower()}_semantic_sample.csv"
        fields = list(rows[0]) if rows else []
        with output.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
        summary[schema] = {
            "sample_count": len(rows),
            "site_count": len({row["site_id"] for row in rows}),
            "change_type_counts": dict(Counter(row["change_type"] for row in rows)),
            "kks_like_count": sum(bool(row["kks_like_candidate"]) for row in rows),
            "file": output.name,
        }
    connection.close()
    result = {
        "run_id": f"source-scoped-semantic-samples-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}",
        "baseline_id": manifest.get("baseline_id"),
        "result_root": str(root),
        "target_per_source": args.target_per_source,
        "summary": summary,
        "preview_only": True,
        "source_write": False,
        "formal_publication": False,
        "next_gate": "replay and review HD/XNY samples before activating any description rule",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    (output_dir / "manifest.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
