"""Resume-safe, read-only extraction of inspection, defect and work-order samples.

The full ASSET snapshot can be large.  This companion extractor intentionally
does not read ASSET rows again; it reads SITEID values from the local ASSET CSV
and opens a fresh DM8 read-only connection for each operational table.  Every
completed table is recorded in a checkpoint manifest so one problematic table
cannot invalidate the rest of the snapshot.
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
MAX_ROWS_PER_SITE = 120

TABLES: dict[str, tuple[str, ...]] = {
    "inspection": ("XJJL", "PLUSCSPOTCHECK", "CHECKS", "ST_USECURITYCHECK", "INVENTORYCHECK", "INVENTORYCHECKMAIN"),
    "defect": ("C_FAULT", "C_FAULTINFO", "C_FAULTDF", "ST_UDEFECTMANAGEMENT", "TICKET"),
    "work_order": ("WORKORDER",),
}

EVENT_FIELDS = (
    "ASSETID", "ASSETNUM", "ASSETNUM1", "STDASSETNUM", "EQUIPMENT_ID", "EQUIPMENTID", "KKS",
    "C_FAULTKKS", "SITEID", "ASSETSITEID", "LOCATION", "WORKLOCATION", "DESCRIPTION", "FAULT_DESC",
    "C_FAULTINFODES", "GZMS", "XQDESCRIPTION", "DEFECTSCONTENT", "WONUM", "WORKORDERID", "TICKETID",
    "C_FAULTID", "C_FAULTNO", "C_FAULTNUM", "XJJLID", "PLUSCSPOTCHECKID", "REPORTDATE", "C_FAULTDATE",
    "INSPECTION_DATE", "CHECKDATE", "CREATEDATE", "C_CREATEDATE", "CHANGEDATE", "STATUS", "CLASSSTRUCTUREID",
)

# Broader inspection fields are retained for traceability and deterministic
# bridge checks. A business record number alone is never a device identity.
INSPECTION_EXTRA_FIELDS = (
    "XJJLNUM", "CHECKID", "CHECKSID", "CHECKNUM", "CHECKITEMID", "ITEMNUM", "ITEMNO",
    "INVENTORYCHECKID", "INVENTORYCHECKMAINID", "INVCHECKNUM", "ST_USECURITYCHECKID",
    "ST_USECURITYCHECKNUM", "STANDINGBOOKNUM", "CHECKUNIT", "AREA", "PROBLEMDESCRIPTION",
    "REMARK", "KKS_CODE", "KKSNUM", "PARENT", "ORGID", "ASSETID", "WORKLOCATION",
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
        raise SystemExit("Missing DMDBMS_BIN")
    os.add_dll_directory(dmdbms_bin)
    try:
        import dmPython  # type: ignore
    except ImportError as exc:
        raise SystemExit("dmPython could not load") from exc
    return dmPython


def connect(dm_python, schema: str, dsn_env: str, user_env: str, password_env: str):
    dsn = os.environ.get(dsn_env)
    password = os.environ.get(password_env)
    user = os.environ.get(user_env, schema)
    if not dsn or not password:
        raise SystemExit(f"Missing {dsn_env} or {password_env}")
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


def site_values(asset_file: pathlib.Path) -> list[str]:
    with asset_file.open("r", encoding="utf-8-sig", newline="") as handle:
        sites = {clean(row.get("SITEID")) for row in csv.DictReader(handle)}
    return sorted(site for site in sites if site)


def write_checkpoint(path: pathlib.Path, manifest: dict[str, object]) -> None:
    if path.exists():
        try:
            prior = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            prior = {}
        if prior.get("run_id") and prior.get("run_id") != manifest.get("run_id"):
            raise RuntimeError(
                f"refusing to overwrite checkpoint {path} for run {prior.get('run_id')} "
                f"with run {manifest.get('run_id')}"
            )
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def extract_table(dm_python, schema: str, dsn_env: str, user_env: str, password_env: str,
                  group: str, table: str, sites: list[str], output: pathlib.Path,
                  extra_fields: tuple[str, ...] = ()) -> dict[str, object]:
    connection = connect(dm_python, schema, dsn_env, user_env, password_env)
    try:
        cursor = connection.cursor()
        cursor.execute("SELECT TABLE_NAME FROM USER_TABLES")
        available = {clean(row[0]).upper() for row in cursor.fetchall()}
        cursor.close()
        if table not in available:
            return {"schema": schema, "group": group, "table": table, "status": "not_found", "rows": 0}
        columns = table_columns(connection, table)
        available_columns = set(columns)
        fields = [field for field in (*EVENT_FIELDS, *extra_fields) if field in available_columns]
        fields = list(dict.fromkeys(fields))
        if not fields:
            return {"schema": schema, "group": group, "table": table, "status": "no_identity_fields", "rows": 0}
        fields_sql = ",".join(quote_ident(field) for field in fields)
        total = 0
        with output.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(fields)
            target_sites = sites if "SITEID" in columns and sites else [""]
            for site in target_sites:
                sql = f"SELECT {fields_sql} FROM {quote_ident(table)}"
                if site:
                    sql += f" WHERE {quote_ident('SITEID')}={quote(site)}"
                sql += f' AND ROWNUM <= {MAX_ROWS_PER_SITE}' if " WHERE " in sql else f' WHERE ROWNUM <= {MAX_ROWS_PER_SITE}'
                cursor = connection.cursor()
                cursor.execute(sql)
                while True:
                    rows = cursor.fetchmany(500)
                    if not rows:
                        break
                    writer.writerows([[clean(value) for value in row] for row in rows])
                    total += len(rows)
                cursor.close()
        return {"schema": schema, "group": group, "table": table, "status": "ok", "rows": total, "file": output.name, "fields": fields}
    finally:
        connection.close()


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--inspection-only", action="store_true")
    args = parser.parse_args()
    snapshot_root = pathlib.Path(os.environ.get("IDENTITY_ASSET_SNAPSHOT", ""))
    if not snapshot_root:
        candidates = sorted((ROOT / "snapshots").glob("identity-source-snapshot-*/"), reverse=True)
        if not candidates:
            raise SystemExit("No local ASSET snapshot found")
        snapshot_root = candidates[0]
    if not snapshot_root.is_absolute():
        snapshot_root = (REPO_ROOT / snapshot_root).resolve()
    run_id = f"identity-operational-snapshot-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    output_dir = ROOT / "snapshots" / run_id
    output_dir.mkdir(parents=True, exist_ok=False)
    manifest: dict[str, object] = {
        "run_id": run_id,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "asset_snapshot": str(snapshot_root),
        "records": [],
        "read_only": True,
        "source_write": False,
        "formal_publication": False,
        "sample_policy": f"operational tables max {MAX_ROWS_PER_SITE} rows per SITEID",
        "inspection_extra_fields": list(INSPECTION_EXTRA_FIELDS) if args.inspection_only else [],
    }
    manifest_path = output_dir / "manifest.json"
    write_checkpoint(manifest_path, manifest)
    dm_python = load_dm_python()
    for schema, dsn_env, user_env, password_env in (
        ("HD_SAAS", "HD_DM_DSN", "HD_DM_USER", "HD_DM_PASSWORD"),
        ("XNY_SAAS", "XNY_DM_DSN", "XNY_DM_USER", "XNY_DM_PASSWORD"),
    ):
        asset_file = snapshot_root / f"{schema.lower()}__asset_master.csv"
        sites = site_values(asset_file) if asset_file.exists() else []
        selected_tables = {"inspection": TABLES["inspection"]} if args.inspection_only else TABLES
        for group, tables in selected_tables.items():
            for table in tables:
                output = output_dir / f"{schema.lower()}__{group}__{table.lower()}.csv"
                try:
                    extras = INSPECTION_EXTRA_FIELDS if group == "inspection" else ()
                    record = extract_table(dm_python, schema, dsn_env, user_env, password_env, group, table, sites, output, extras)
                except Exception as exc:
                    record = {"schema": schema, "group": group, "table": table, "status": "failed", "error_type": type(exc).__name__}
                records = manifest["records"]
                assert isinstance(records, list)
                records.append(record)
                write_checkpoint(manifest_path, manifest)
    manifest["finished_at"] = datetime.now(timezone.utc).isoformat()
    write_checkpoint(manifest_path, manifest)
    print(json.dumps({"run_id": run_id, "records": manifest["records"], "read_only": True}, ensure_ascii=False))


if __name__ == "__main__":
    main()
