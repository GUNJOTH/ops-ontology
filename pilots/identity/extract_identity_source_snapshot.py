"""Capture a bounded, read-only source snapshot for identity matching.

ASSET is captured at full identity grain.  Operational tables are sampled per
SITEID so the first identity run remains bounded and auditable.  The source
tables are never updated and credentials are read only from environment vars.
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
import pathlib
import re
import sys
from datetime import datetime, timezone


ROOT = pathlib.Path(__file__).resolve().parent
REPO_ROOT = ROOT.parents[2]
sys.path.insert(0, str(REPO_ROOT / "equipment-description-harness" / "tools" / "dmPython-src"))
IDENTIFIER = re.compile(r"^[A-Z][A-Z0-9_$#]{0,127}$")
MAX_OPERATIONAL_ROWS_PER_SITE = 250

TABLES: dict[str, tuple[str, ...]] = {
    "inspection": ("XJJL", "PLUSCSPOTCHECK", "CHECKS", "ST_USECURITYCHECK", "INVENTORYCHECK", "INVENTORYCHECKMAIN"),
    "defect": ("C_FAULT", "C_FAULTINFO", "C_FAULTDF", "ST_UDEFECTMANAGEMENT", "TICKET"),
    "work_order": ("WORKORDER",),
}

ASSET_FIELDS = (
    "ASSETID", "SITEID", "ORGID", "ASSETNUM", "DESCRIPTION", "PARENT", "LOCATION", "STATUS",
    "SERIALNUM", "MANUFACTURER", "ITEMNUM", "CLASSSTRUCTUREID", "CLASSIFICATIONID",
    "PLUSCMODELNUM", "S_MODELNUM", "MODELNUM", "S_OLDASSETNUM", "C_OLDLOCATION", "CHANGEDATE",
)
EVENT_FIELDS = (
    "ASSETID", "ASSETNUM", "ASSETNUM1", "STDASSETNUM", "EQUIPMENT_ID", "EQUIPMENTID", "KKS",
    "C_FAULTKKS", "SITEID", "ASSETSITEID", "LOCATION", "WORKLOCATION", "DESCRIPTION", "FAULT_DESC",
    "C_FAULTINFODES", "GZMS", "XQDESCRIPTION", "DEFECTSCONTENT", "WONUM", "WORKORDERID", "TICKETID",
    "C_FAULTID", "C_FAULTNO", "C_FAULTNUM", "XJJLID", "PLUSCSPOTCHECKID", "REPORTDATE", "C_FAULTDATE",
    "INSPECTION_DATE", "CHECKDATE", "CREATEDATE", "C_CREATEDATE", "CHANGEDATE", "STATUS", "CLASSSTRUCTUREID",
)


def clean(value: object) -> str:
    return "" if value is None else str(value).strip()


def quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def quote_ident(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def load_dm_python():
    dmdbms_bin = os.environ.get("DMDBMS_BIN")
    if not dmdbms_bin or not pathlib.Path(dmdbms_bin).is_dir():
        raise SystemExit("Missing DMDBMS_BIN; set it to the DM8 client bin directory.")
    os.add_dll_directory(dmdbms_bin)
    try:
        import dmPython  # type: ignore
    except ImportError as exc:
        raise SystemExit("dmPython could not load; verify the Python version and DM8 client.") from exc
    return dmPython


def connect(dm_python, schema: str, dsn_env: str, user_env: str, password_env: str):
    dsn = os.environ.get(dsn_env)
    password = os.environ.get(password_env)
    user = os.environ.get(user_env, schema)
    if not dsn or not password:
        raise SystemExit(f"Missing {dsn_env} or {password_env}; set local read-only connection variables.")
    return dm_python.connect(
        user=user,
        password=password,
        dsn=dsn,
        schema=schema,
        access_mode=dm_python.DSQL_MODE_READ_ONLY,
        autoCommit=True,
    )


def table_columns(connection, table: str) -> list[str]:
    if not IDENTIFIER.fullmatch(table):
        return []
    cursor = connection.cursor()
    cursor.execute(f"SELECT * FROM {quote_ident(table)} WHERE 1=0")
    columns = [clean(item[0]).upper() for item in (cursor.description or [])]
    cursor.close()
    return columns


def select_fields(columns: list[str], preferred: tuple[str, ...]) -> list[str]:
    available = set(columns)
    return [field for field in preferred if field in available]


def write_query_rows(connection, sql: str, fields: list[str], output: pathlib.Path) -> int:
    cursor = connection.cursor()
    cursor.execute(sql)
    count = 0
    with output.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(fields)
        while True:
            rows = cursor.fetchmany(500)
            if not rows:
                break
            writer.writerows([[clean(value) for value in row] for row in rows])
            count += len(rows)
    cursor.close()
    return count


def capture_schema(dm_python, schema: str, dsn_env: str, user_env: str, password_env: str, output_dir: pathlib.Path) -> list[dict[str, object]]:
    connection = connect(dm_python, schema, dsn_env, user_env, password_env)
    records: list[dict[str, object]] = []
    try:
        cursor = connection.cursor()
        cursor.execute("SELECT TABLE_NAME FROM USER_TABLES")
        available = {clean(row[0]).upper() for row in cursor.fetchall()}
        cursor.close()
        asset_columns = table_columns(connection, "ASSET") if "ASSET" in available else []
        asset_fields = select_fields(asset_columns, ASSET_FIELDS)
        if asset_fields:
            output = output_dir / f"{schema.lower()}__asset_master.csv"
            fields_sql = ",".join(quote_ident(field) for field in asset_fields)
            count = write_query_rows(connection, f"SELECT {fields_sql} FROM {quote_ident('ASSET')} ORDER BY {quote_ident('SITEID')},{quote_ident('ASSETNUM')}", asset_fields, output)
            records.append({"schema": schema, "group": "asset_master", "table": "ASSET", "rows": count, "file": output.name, "fields": asset_fields})

        site_values: list[str] = []
        if asset_columns and "SITEID" in asset_columns:
            cursor = connection.cursor()
            cursor.execute('SELECT DISTINCT "SITEID" FROM "ASSET" WHERE "SITEID" IS NOT NULL ORDER BY "SITEID"')
            site_values = [clean(row[0]) for row in cursor.fetchall() if clean(row[0])]
            cursor.close()
        for group, tables in TABLES.items():
            for table in tables:
                if table not in available:
                    continue
                columns = table_columns(connection, table)
                fields = select_fields(columns, EVENT_FIELDS)
                if not fields:
                    continue
                fields_sql = ",".join(quote_ident(field) for field in fields)
                output = output_dir / f"{schema.lower()}__{group}__{table.lower()}.csv"
                total = 0
                if "SITEID" in columns and site_values:
                    with output.open("w", encoding="utf-8-sig", newline="") as handle:
                        writer = csv.writer(handle)
                        writer.writerow(fields)
                        for site in site_values:
                            sql = f"SELECT {fields_sql} FROM {quote_ident(table)} WHERE {quote_ident('SITEID')}={quote(site)} AND ROWNUM <= {MAX_OPERATIONAL_ROWS_PER_SITE}"
                            cursor = connection.cursor()
                            cursor.execute(sql)
                            while True:
                                rows = cursor.fetchmany(500)
                                if not rows:
                                    break
                                writer.writerows([[clean(value) for value in row] for row in rows])
                                total += len(rows)
                            cursor.close()
                else:
                    total = write_query_rows(connection, f"SELECT {fields_sql} FROM {quote_ident(table)} WHERE ROWNUM <= {MAX_OPERATIONAL_ROWS_PER_SITE}", fields, output)
                records.append({"schema": schema, "group": group, "table": table, "rows": total, "file": output.name, "fields": fields})
    finally:
        connection.close()
    return records


def main() -> None:
    dm_python = load_dm_python()
    captured_at = datetime.now(timezone.utc)
    run_id = f"identity-source-snapshot-{captured_at.strftime('%Y%m%dT%H%M%SZ')}"
    output_dir = ROOT / "snapshots" / run_id
    output_dir.mkdir(parents=True, exist_ok=False)
    records: list[dict[str, object]] = []
    schema_status: list[dict[str, object]] = []
    for schema, dsn_env, user_env, password_env in (
        ("HD_SAAS", "HD_DM_DSN", "HD_DM_USER", "HD_DM_PASSWORD"),
        ("XNY_SAAS", "XNY_DM_DSN", "XNY_DM_USER", "XNY_DM_PASSWORD"),
    ):
        try:
            schema_records = capture_schema(dm_python, schema, dsn_env, user_env, password_env, output_dir)
            records.extend(schema_records)
            schema_status.append({"schema": schema, "status": "ok", "table_count": len(schema_records)})
        except Exception as exc:
            schema_status.append({"schema": schema, "status": "failed", "error_type": type(exc).__name__})
    manifest = {
        "run_id": run_id,
        "captured_at": captured_at.isoformat(),
        "schemas": schema_status,
        "records": records,
        "read_only": True,
        "source_write": False,
        "formal_publication": False,
        "sample_policy": f"ASSET full; operational tables max {MAX_OPERATIONAL_ROWS_PER_SITE} rows per SITEID",
        "next_step": "build local unified_device and device_identity_map",
    }
    (output_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"run_id": run_id, "schemas": schema_status, "records": [{"schema": item["schema"], "group": item["group"], "table": item["table"], "rows": item["rows"]} for item in records], "read_only": True}, ensure_ascii=False))


if __name__ == "__main__":
    main()
