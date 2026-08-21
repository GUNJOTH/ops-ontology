"""Inventory source fields for cross-system device identity matching.

The script is intentionally read-only.  It checks which operational tables
exist in HD_SAAS/XNY_SAAS, records their columns and classifies likely identity,
context and event fields.  It does not export business rows or alter DM8.

Connection values must be supplied through environment variables:
HD_DM_DSN/HD_DM_USER/HD_DM_PASSWORD and XNY_DM_DSN/XNY_DM_USER/XNY_DM_PASSWORD.
"""
from __future__ import annotations

import csv
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

TABLE_GROUPS: dict[str, tuple[str, ...]] = {
    "asset_master": ("ASSET",),
    "inspection": (
        "XJJL", "PLUSCSPOTCHECK", "CHECKS", "CHECKITEM", "ST_USECURITYCHECK",
        "INVENTORYCHECK", "INVENTORYCHECKMAIN",
    ),
    "defect": (
        "C_FAULT", "C_FAULTINFO", "C_FAULTDF", "SR", "TICKET",
        "ST_UDEFECTCOMBED", "ST_UDEFECTMANAGEMENT",
    ),
    "work_order": ("WORKORDER",),
    "device_context": (
        "LOCATIONS", "LOCHIERARCHY", "ASSETSPEC", "ASSETFEATURE", "ASSETATTRIBUTE",
        "CLASSIFICATION", "CLASSSTRUCTURE", "ASSETMETER", "ASSETHIERARCHY", "ASSETLOCRELATION",
    ),
}

ROLE_TOKENS: dict[str, tuple[str, ...]] = {
    "source_identity": (
        "ASSETID", "ASSETNUM", "ASSET_CODE", "ASSETCODE", "EQUIPMENT_ID",
        "EQUIPMENTID", "EQUIP_ID", "EQUIPID", "KKS", "KKS_CODE", "KKS码",
    ),
    "site": ("SITEID", "SITE_ID", "SITE_CODE", "PLANT", "PLANT_CODE", "厂站"),
    "location": ("LOCATION", "LOCATION_ID", "LOCATION_CODE", "LOC", "LOCID", "位置"),
    "description": (
        "DESCRIPTION", "DESCRIP", "FAULT_DESC", "FAULTDESCRIPTION", "TITLE",
        "SUMMARY", "REMARK", "MEMO", "故障", "缺陷", "描述",
    ),
    "event_id": (
        "TICKETID", "SRID", "WONUM", "WORKORDERID", "WORKORDER_ID", "CHECKID",
        "INSPECTION_ID", "FAULTID", "FAULT_ID",
    ),
    "event_time": (
        "REPORTDATE", "REPORT_TIME", "INSPECTION_DATE", "CHECKDATE", "FAULTDATE",
        "CHANGEDATE", "CREATEDATE", "CREATE_TIME", "OBSERVATIONDATE",
    ),
    "classification": ("CLASSSTRUCTUREID", "CLASSIFICATIONID", "EQUIP_TYPE", "TYPE_CODE", "分类"),
    "measurement": ("TEMPERATURE", "VALUE", "MEASURE", "READING", "OBSERVATION", "测量", "温度"),
    "status": ("STATUS", "STATE", "ISDEL", "HISTORYFLAG"),
}


def clean(value: object) -> str:
    return "" if value is None else str(value).strip()


def quote_ident(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def write_manifest(path: pathlib.Path, manifest: dict[str, object]) -> None:
    if path.exists():
        try:
            prior = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            prior = {}
        if prior.get("run_id") and prior.get("run_id") != manifest.get("run_id"):
            raise RuntimeError(
                f"refusing to overwrite manifest {path} for run {prior.get('run_id')} "
                f"with run {manifest.get('run_id')}"
            )
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


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


def table_candidates() -> dict[str, str]:
    return {table: group for group, tables in TABLE_GROUPS.items() for table in tables}


def classify_column(name: str) -> list[str]:
    upper = name.upper()
    roles: list[str] = []
    for role, tokens in ROLE_TOKENS.items():
        if upper in {token.upper() for token in tokens} or any(token.upper() in upper for token in tokens if len(token) >= 5):
            roles.append(role)
    return roles


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


def inventory_schema(dm_python, schema: str, dsn_env: str, user_env: str, password_env: str) -> list[dict[str, object]]:
    connection = connect(dm_python, schema, dsn_env, user_env, password_env)
    candidates = table_candidates()
    rows: list[dict[str, object]] = []
    try:
        cursor = connection.cursor()
        cursor.execute("SELECT TABLE_NAME FROM USER_TABLES")
        available = {clean(row[0]).upper() for row in cursor.fetchall()}
        cursor.close()
        for table, group in candidates.items():
            if table not in available:
                continue
            if not IDENTIFIER.fullmatch(table):
                continue
            cursor = connection.cursor()
            cursor.execute(f"SELECT * FROM {quote_ident(table)} WHERE 1=0")
            descriptions = list(cursor.description or [])
            cursor.close()
            for ordinal, item in enumerate(descriptions, start=1):
                column = clean(item[0]).upper()
                rows.append({
                    "schema": schema,
                    "table_group": group,
                    "table_name": table,
                    "column_name": column,
                    "ordinal": ordinal,
                    "roles": ";".join(classify_column(column)),
                })
    finally:
        connection.close()
    return rows


def main() -> None:
    dm_python = load_dm_python()
    captured_at = datetime.now(timezone.utc)
    run_id = f"identity-source-inventory-{captured_at.strftime('%Y%m%dT%H%M%SZ')}"
    output_dir = ROOT / "inventory" / run_id
    output_dir.mkdir(parents=True, exist_ok=False)
    schemas = (
        ("HD_SAAS", "HD_DM_DSN", "HD_DM_USER", "HD_DM_PASSWORD"),
        ("XNY_SAAS", "XNY_DM_DSN", "XNY_DM_USER", "XNY_DM_PASSWORD"),
    )
    all_rows: list[dict[str, object]] = []
    schema_status: list[dict[str, object]] = []
    for schema, dsn_env, user_env, password_env in schemas:
        try:
            rows = inventory_schema(dm_python, schema, dsn_env, user_env, password_env)
            all_rows.extend(rows)
            schema_status.append({"schema": schema, "status": "ok", "field_count": len(rows)})
        except Exception as exc:  # keep the other schema's inventory available
            schema_status.append({"schema": schema, "status": "failed", "error_type": type(exc).__name__})

    csv_path = output_dir / "source_identity_field_inventory.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["schema", "table_group", "table_name", "column_name", "ordinal", "roles"])
        writer.writeheader()
        writer.writerows(all_rows)
    manifest = {
        "run_id": run_id,
        "captured_at": captured_at.isoformat(),
        "schemas": schema_status,
        "table_groups": TABLE_GROUPS,
        "row_count": len(all_rows),
        "read_only": True,
        "source_write": False,
        "formal_publication": False,
        "next_step": "build local unified_device and device_identity_map from confirmed fields",
    }
    write_manifest(output_dir / "manifest.json", manifest)
    print(json.dumps(manifest, ensure_ascii=False))


if __name__ == "__main__":
    main()
