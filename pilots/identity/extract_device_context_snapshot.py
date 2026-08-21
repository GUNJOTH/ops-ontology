"""Extract location, hierarchy, specification and feature context read-only."""
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
MAX_ROWS_PER_TABLE = 5000

TABLE_FIELDS: dict[str, tuple[str, ...]] = {
    "LOCATIONS": ("LOCATIONSID", "LOCATION", "LOCATIONSCODE", "DESCRIPTION", "PARENT", "SITEID", "ORGID", "STATUS", "CLASSSTRUCTUREID", "CHANGEDATE"),
    "LOCHIERARCHY": ("LOCHIERARCHYID", "LOCATION", "PARENT", "SITEID", "ORGID"),
    "ASSETSPEC": ("ASSETSPECID", "ASSETNUM", "ASSETATTRID", "CLASSSTRUCTUREID", "SITEID", "ORGID", "ALNVALUE", "NUMVALUE", "TABLEVALUE", "CHANGEDATE"),
    "ASSETFEATURE": ("ASSETFEATUREID", "ASSETNUM", "ASSETUID", "FEATURESID", "FEATURE", "CLASSSTRUCTUREID", "SITEID", "ORGID", "STARTFEATURE", "ENDFEATURE", "CHANGEDATE"),
    "ASSETATTRIBUTE": ("ASSETATTRIBUTEID", "ASSETATTRID", "DESCRIPTION", "DATATYPE", "MEASUREUNITID", "DOMAINID", "SITEID", "ORGID"),
    "CLASSIFICATION": ("CLASSIFICATIONID", "DESCRIPTION", "SITEID", "ORGID"),
    "CLASSSTRUCTURE": ("CLASSSTRUCTUREID", "CLASSIFICATIONID", "DESCRIPTION", "DESCRIPTION_CLASS", "GENASSETDESC", "PARENT", "SITEID", "ORGID"),
    "ASSETMETER": ("ASSETMETERID", "ASSETNUM", "METERNAME", "SITEID", "ORGID", "LASTREADING", "LASTREADINGDATE", "LIFETODATE", "SINCELASTINSPECT", "CHANGEDATE"),
    "ASSETHIERARCHY": ("ASSETHIERARCHYID", "ASSETNUM", "PARENT", "LOCATION", "SITEID", "ORGID"),
    "ASSETLOCRELATION": ("ASSETLOCRELATIONID", "ASSETRELATIONNUM", "SOURCEASSETNUM", "SOURCELOCATION", "TARGETASSETNUM", "TARGETLOCATION", "SITEID", "ORGID", "CREATEDDATE"),
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
    return dm_python.connect(user=user, password=password, dsn=dsn, schema=schema,
                             access_mode=dm_python.DSQL_MODE_READ_ONLY, autoCommit=True)


def table_columns(connection, table: str) -> list[str]:
    if not IDENTIFIER.fullmatch(table):
        return []
    cursor = connection.cursor()
    cursor.execute(f"SELECT * FROM {quote_ident(table)} WHERE 1=0")
    columns = [clean(item[0]).upper() for item in (cursor.description or [])]
    cursor.close()
    return columns


def extract_table(dm_python, schema: str, dsn_env: str, user_env: str, password_env: str,
                  table: str, output: pathlib.Path) -> dict[str, object]:
    connection = connect(dm_python, schema, dsn_env, user_env, password_env)
    try:
        cursor = connection.cursor()
        cursor.execute("SELECT TABLE_NAME FROM USER_TABLES")
        available = {clean(row[0]).upper() for row in cursor.fetchall()}
        cursor.close()
        if table not in available:
            return {"schema": schema, "table": table, "status": "not_found", "rows": 0}
        columns = table_columns(connection, table)
        fields = [field for field in TABLE_FIELDS[table] if field in set(columns)]
        if not fields:
            return {"schema": schema, "table": table, "status": "no_selected_fields", "rows": 0}
        fields_sql = ",".join(quote_ident(field) for field in fields)
        sql = f"SELECT {fields_sql} FROM {quote_ident(table)} WHERE ROWNUM <= {MAX_ROWS_PER_TABLE}"
        cursor = connection.cursor()
        cursor.execute(sql)
        count = 0
        with output.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(fields)
            while True:
                rows = cursor.fetchmany(1000)
                if not rows:
                    break
                writer.writerows([[clean(value) for value in row] for row in rows])
                count += len(rows)
        cursor.close()
        return {"schema": schema, "table": table, "status": "ok", "rows": count, "file": output.name, "fields": fields}
    finally:
        connection.close()


def main() -> None:
    dm_python = load_dm_python()
    captured_at = datetime.now(timezone.utc)
    run_id = f"identity-context-snapshot-{captured_at.strftime('%Y%m%dT%H%M%SZ')}"
    output_dir = ROOT / "snapshots" / run_id
    output_dir.mkdir(parents=True, exist_ok=False)
    manifest: dict[str, object] = {"run_id": run_id, "started_at": captured_at.isoformat(), "records": [],
                                   "read_only": True, "source_write": False, "formal_publication": False,
                                   "sample_policy": f"context tables max {MAX_ROWS_PER_TABLE} rows per table"}
    manifest_path = output_dir / "manifest.json"
    write_manifest(manifest_path, manifest)
    for schema, dsn_env, user_env, password_env in (
        ("HD_SAAS", "HD_DM_DSN", "HD_DM_USER", "HD_DM_PASSWORD"),
        ("XNY_SAAS", "XNY_DM_DSN", "XNY_DM_USER", "XNY_DM_PASSWORD"),
    ):
        for table in TABLE_FIELDS:
            output = output_dir / f"{schema.lower()}__context__{table.lower()}.csv"
            try:
                record = extract_table(dm_python, schema, dsn_env, user_env, password_env, table, output)
            except Exception as exc:
                record = {"schema": schema, "table": table, "status": "failed", "error_type": type(exc).__name__}
            records = manifest["records"]
            assert isinstance(records, list)
            records.append(record)
            write_manifest(manifest_path, manifest)
    manifest["finished_at"] = datetime.now(timezone.utc).isoformat()
    write_manifest(manifest_path, manifest)
    print(json.dumps({"run_id": run_id, "records": manifest["records"], "read_only": True}, ensure_ascii=False))


if __name__ == "__main__":
    main()
