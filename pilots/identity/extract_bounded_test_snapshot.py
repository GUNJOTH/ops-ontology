"""Extract a bounded, stratified read-only test snapshot from HD/XNY DM8.

This is deliberately separate from the historical full-source snapshot job.
It creates a reproducible-enough development baseline without copying the
production-sized ASSET tables.  The source connections are read-only and the
output is local evidence only; no source table is changed.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import pathlib
import sys
from collections import Counter
from datetime import datetime, timezone
from typing import Any


ROOT = pathlib.Path(__file__).resolve().parent
REPO_ROOT = ROOT.parents[2]
DM_SOURCE = REPO_ROOT / "equipment-description-harness" / "tools" / "dmPython-src"
ASSET_FIELDS = [
    "ASSETID", "SITEID", "ORGID", "ASSETNUM", "DESCRIPTION", "PARENT", "LOCATION", "STATUS",
    "SERIALNUM", "MANUFACTURER", "ITEMNUM", "CLASSSTRUCTUREID", "CLASSIFICATIONID",
    "ASSETTYPE", "PLUSCMODELNUM", "S_MODELNUM", "MODELNUM", "S_OLDASSETNUM",
    "C_OLDLOCATION", "CHANGEDATE",
]
OPERATIONAL_TABLES: dict[str, tuple[str, ...]] = {
    "inspection": ("XJJL",),
    "defect": ("C_FAULT", "SR"),
    "work_order": ("WORKORDER",),
    "device_context": ("LOCATIONS", "LOCHIERARCHY", "ASSETSPEC", "ASSETFEATURE", "ASSETATTRIBUTE", "CLASSIFICATION", "CLASSSTRUCTURE", "ASSETMETER", "ASSETHIERARCHY", "ASSETLOCRELATION"),
}
MAX_COLUMNS = 48


def clean(value: object) -> str:
    return "" if value is None else str(value).strip()


def quote_ident(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def json_default(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, (bytes, bytearray)):
        return value.hex()
    return str(value)


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_dm_python():
    dmdbms_bin = os.environ.get("DMDBMS_BIN") or r"D:\dmdbms\bin"
    if pathlib.Path(dmdbms_bin).is_dir():
        os.add_dll_directory(dmdbms_bin)
    sys.path.insert(0, str(DM_SOURCE))
    try:
        import dmPython  # type: ignore
    except Exception as exc:  # pragma: no cover - environment-specific
        raise SystemExit(
            "dmPython 加载失败；请使用 Python 3.11，并确认 DMDBMS_BIN 指向 DM8 bin。"
        ) from exc
    return dmPython


def connect(dm_python: Any, schema: str):
    prefix = "HD" if schema == "HD_SAAS" else "XNY"
    dsn = os.environ.get(f"{prefix}_DM_DSN")
    user = os.environ.get(f"{prefix}_DM_USER", schema)
    password = os.environ.get(f"{prefix}_DM_PASSWORD")
    if not dsn or not password:
        raise SystemExit(f"缺少 {prefix}_DM_DSN 或 {prefix}_DM_PASSWORD 环境变量")
    return dm_python.connect(
        user=user,
        password=password,
        dsn=dsn,
        schema=schema,
        access_mode=dm_python.DSQL_MODE_READ_ONLY,
        autoCommit=True,
    )


def table_columns(connection: Any, table: str, *, limit: int | None = MAX_COLUMNS) -> list[str]:
    rows = connection.cursor()
    try:
        rows.execute(
            "SELECT COLUMN_NAME FROM USER_TAB_COLUMNS "
            "WHERE TABLE_NAME=? ORDER BY COLUMN_ID",
            (table,),
        )
        fields = [str(row[0]) for row in rows.fetchall()]
        return fields if limit is None else fields[:limit]
    finally:
        rows.close()


def site_counts(connection: Any) -> list[tuple[str, int]]:
    cursor = connection.cursor()
    try:
        cursor.execute(
            "SELECT SITEID, COUNT(*) FROM ASSET "
            "GROUP BY SITEID ORDER BY SITEID"
        )
        return [(clean(row[0]) or "__NULL_SITE__", int(row[1])) for row in cursor.fetchall()]
    finally:
        cursor.close()


def allocate(counts: list[tuple[str, int]], target: int) -> dict[str, int]:
    total = sum(count for _, count in counts)
    if total <= 0:
        return {}
    target = min(target, total)
    raw = [(site, target * count / total) for site, count in counts]
    result = {site: min(count, int(value)) for (site, count), (_, value) in zip(counts, raw)}
    remainder = target - sum(result.values())
    order = sorted(
        ((site, count, value - int(value)) for (site, count), (_, value) in zip(counts, raw)),
        key=lambda item: (-item[2], item[0]),
    )
    for site, count, _fraction in order:
        if remainder <= 0:
            break
        if result[site] < count:
            result[site] += 1
            remainder -= 1
    return {site: count for site, count in result.items() if count > 0}


def fetch_asset_rows(connection: Any, per_site: dict[str, int]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    available = set(table_columns(connection, "ASSET", limit=None))
    selected_fields = [field for field in ASSET_FIELDS if field in available]
    if not selected_fields:
        raise RuntimeError("ASSET 没有可用的标准身份字段")
    fields_sql = ",".join(quote_ident(field) for field in selected_fields)
    for site, limit in sorted(per_site.items()):
        cursor = connection.cursor()
        try:
            if site == "__NULL_SITE__":
                predicate = "SITEID IS NULL"
                params: tuple[object, ...] = ()
            else:
                predicate = "SITEID=?"
                params = (site,)
            cursor.execute(
                f"SELECT {fields_sql} FROM (SELECT {fields_sql} FROM ASSET "
                f"WHERE {predicate} ORDER BY ASSETNUM) WHERE ROWNUM <= ?",
                (*params, limit),
            )
            for values in cursor.fetchall():
                row = {field: "" for field in ASSET_FIELDS}
                row.update({field: json_default(value) for field, value in zip(selected_fields, values)})
                rows.append(row)
        finally:
            cursor.close()
    return rows


def write_csv(path: pathlib.Path, fields: list[str], rows: list[dict[str, str]]) -> dict[str, object]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return {"file": path.name, "rows": len(rows), "sha256": sha256_file(path)}


def fetch_operational_rows(connection: Any, table: str, limit: int, sites: list[str]) -> tuple[list[str], list[dict[str, str]]]:
    columns = table_columns(connection, table)
    if not columns:
        return [], []
    selected = columns
    fields_sql = ",".join(quote_ident(field) for field in selected)
    site_field = next((field for field in selected if field.upper() in {"SITEID", "ASSETSITEID", "WORKSITEID"}), None)
    if site_field and sites:
        placeholders = ",".join("?" for _ in sites)
        sql = f"SELECT {fields_sql} FROM {quote_ident(table)} WHERE {quote_ident(site_field)} IN ({placeholders}) AND ROWNUM <= ?"
        params: tuple[object, ...] = (*sites, limit)
    else:
        sql = f"SELECT {fields_sql} FROM {quote_ident(table)} WHERE ROWNUM <= ?"
        params = (limit,)
    cursor = connection.cursor()
    try:
        cursor.execute(sql, params)
        rows = [{field: json_default(value) for field, value in zip(selected, values)} for values in cursor.fetchall()]
    finally:
        cursor.close()
    return selected, rows


def capture_schema(dm_python: Any, schema: str, target_assets: int, operational_limit: int, asset_root: pathlib.Path, operational_root: pathlib.Path) -> dict[str, object]:
    connection = connect(dm_python, schema)
    try:
        counts = site_counts(connection)
        allocation = allocate(counts, target_assets)
        asset_rows = fetch_asset_rows(connection, allocation)
        asset_meta = write_csv(asset_root / f"{schema.lower()}__asset_master.csv", ASSET_FIELDS, asset_rows)
        selected_sites = sorted({clean(row.get("SITEID")) for row in asset_rows if clean(row.get("SITEID"))})
        operational: list[dict[str, object]] = []
        for group, tables in OPERATIONAL_TABLES.items():
            for table in tables:
                try:
                    fields, rows = fetch_operational_rows(connection, table, operational_limit, selected_sites)
                except Exception as exc:
                    if "TABLE" in str(exc).upper() or "INVALID" in str(exc).upper():
                        continue
                    raise
                if not fields:
                    continue
                meta = write_csv(operational_root / f"{schema.lower()}__{group}__{table.lower()}.csv", fields, rows)
                operational.append({"group": group, "table": table, **meta})
        return {
            "schema": schema,
            "target_assets": target_assets,
            "asset_count": len(asset_rows),
            "site_count": len(selected_sites),
            "site_counts": dict(Counter(clean(row.get("SITEID")) for row in asset_rows)),
            "asset": asset_meta,
            "operational": operational,
        }
    finally:
        connection.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract bounded HD/XNY read-only semantic test snapshots")
    parser.add_argument("--total", type=int, default=5000)
    parser.add_argument("--operational-limit-per-table", type=int, default=120)
    parser.add_argument("--output-root", type=pathlib.Path, default=ROOT / "snapshots")
    args = parser.parse_args()
    if args.total <= 0:
        raise SystemExit("--total 必须大于 0")
    captured_at = datetime.now(timezone.utc)
    run_stamp = captured_at.strftime("%Y%m%dT%H%M%SZ")
    asset_root = args.output_root / f"identity-test-assets-{args.total}-{run_stamp}"
    operational_root = args.output_root / f"identity-test-operational-{args.total}-{run_stamp}"
    asset_root.mkdir(parents=True, exist_ok=False)
    operational_root.mkdir(parents=True, exist_ok=False)
    dm_python = load_dm_python()
    per_schema = args.total // 2
    targets = {"HD_SAAS": per_schema + args.total % 2, "XNY_SAAS": per_schema}
    summaries = []
    for schema in ("HD_SAAS", "XNY_SAAS"):
        summaries.append(capture_schema(dm_python, schema, targets[schema], args.operational_limit_per_table, asset_root, operational_root))
    manifest = {
        "run_id": f"identity-test-snapshot-{args.total}-{run_stamp}",
        "asset_snapshot_id": asset_root.name,
        "operational_snapshot_id": operational_root.name,
        "captured_at": captured_at.isoformat(),
        "target_total": args.total,
        "asset_row_count": sum(int(item["asset_count"]) for item in summaries),
        "source_domains": ["HD_SAAS", "XNY_SAAS"],
        "summaries": summaries,
        "asset_root": str(asset_root.resolve()),
        "operational_root": str(operational_root.resolve()),
        "read_only": True,
        "source_write": False,
        "formal_publication": False,
    }
    for root, name in ((asset_root, "manifest.json"), (operational_root, "manifest.json")):
        (root / name).write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    manifest_path = args.output_root / f"identity-test-manifest-{args.total}-{run_stamp}.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
