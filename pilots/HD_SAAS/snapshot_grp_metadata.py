"""Capture GRP metadata tables from DM8 in read-only mode.

The script writes a timestamped local snapshot and manifest. It never writes
to the source database and never stores credentials in the output.
"""
from __future__ import annotations

import argparse
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

CORE_TABLES = (
    "GRPOBJECT",
    "GRPATTRIBUTE",
    "GRPRELATIONSHIP",
    "GRPTABLE",
    "GRPVIEW",
    "GRPVIEWCOLUMN",
)
TECHNICAL_TABLES = ("GRPSYSKEYS", "GRPSYSINDEXES")
IDENTIFIER = re.compile(r"^[A-Z][A-Z0-9_$#]{0,127}$")


def clean(value: object) -> str:
    return "" if value is None else str(value)


def quote_ident(value: object) -> str:
    return '"' + str(value).replace('"', '""') + '"'


def sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Capture read-only DM8 GRP metadata tables")
    parser.add_argument("--schema", default="HD_SAAS", help="DM8 schema, for example HD_SAAS or XNY_SAAS")
    parser.add_argument("--dsn-env", default="HD_DM_DSN", help="Environment variable containing host:port")
    parser.add_argument("--user-env", default="HD_DM_USER", help="Environment variable containing the database user")
    parser.add_argument("--password-env", default="HD_DM_PASSWORD", help="Environment variable containing the database password")
    parser.add_argument("--output-root", default=str(ROOT.parent / "metadata" / "grp_metadata_snapshots"), help="Local snapshot output directory")
    parser.add_argument("--include-technical", action="store_true", help="Also capture GRPSYSKEYS and GRPSYSINDEXES")
    return parser.parse_args()


def load_dm_python():
    dmdbms_bin = os.environ.get("DMDBMS_BIN")
    if not dmdbms_bin or not pathlib.Path(dmdbms_bin).is_dir():
        raise SystemExit("Missing DMDBMS_BIN. Set it to the DM8 client bin directory, for example D:\\dmdbms\\bin.")
    os.add_dll_directory(dmdbms_bin)
    try:
        import dmPython
    except ImportError as exc:
        raise SystemExit("dmPython could not load. Use Python 3.11 and verify DMDBMS_BIN points to the DM8 client bin directory.") from exc
    return dmPython


def main() -> None:
    args = parse_args()
    schema = str(args.schema).strip().upper()
    if not IDENTIFIER.fullmatch(schema):
        raise SystemExit(f"Invalid schema identifier: {schema}")
    tables = list(CORE_TABLES) + (list(TECHNICAL_TABLES) if args.include_technical else [])
    dsn = os.environ.get(args.dsn_env)
    user = os.environ.get(args.user_env, schema)
    password = os.environ.get(args.password_env)
    if not dsn or not password:
        raise SystemExit(
            f"Missing read-only connection variables: {args.dsn_env} and {args.password_env}. "
            "Set them in the local shell; do not place credentials in source files."
        )
    dmPython = load_dm_python()

    captured_at = datetime.now(timezone.utc)
    run_id = f"{schema.lower()}-grp-metadata-{captured_at.strftime('%Y%m%dT%H%M%SZ')}"
    output_dir = pathlib.Path(args.output_root) / run_id
    table_manifest: list[dict[str, object]] = []
    errors: list[dict[str, str]] = []

    connection = None
    cursor = None
    try:
        connection = dmPython.connect(
            user=user,
            password=password,
            dsn=dsn,
            schema=schema,
            access_mode=dmPython.DSQL_MODE_READ_ONLY,
            autoCommit=True,
        )
        output_dir.mkdir(parents=True, exist_ok=False)
        cursor = connection.cursor()
        for table in tables:
            target = output_dir / f"{table.lower()}.csv"
            try:
                cursor.execute(f'SELECT * FROM {quote_ident(table)}')
                columns = [str(item[0]) for item in cursor.description]
                row_count = 0
                with target.open("w", encoding="utf-8-sig", newline="") as handle:
                    writer = csv.writer(handle)
                    writer.writerow(columns)
                    while True:
                        rows = cursor.fetchmany(2000)
                        if not rows:
                            break
                        for row in rows:
                            writer.writerow([clean(value) for value in row])
                            row_count += 1
                table_manifest.append(
                    {
                        "table": table,
                        "status": "captured",
                        "path": str(target),
                        "row_count": row_count,
                        "column_count": len(columns),
                        "columns": columns,
                        "sha256": sha256(target),
                    }
                )
            except Exception as exc:  # table-level evidence is preserved in the manifest
                errors.append({"table": table, "error_type": type(exc).__name__, "message": str(exc)[:500]})
                table_manifest.append({"table": table, "status": "failed"})
    finally:
        if cursor is not None:
            cursor.close()
        if connection is not None:
            connection.close()

    snapshot_seed = json.dumps(
        {"schema": schema, "captured_at": captured_at.isoformat(), "tables": table_manifest},
        ensure_ascii=False,
        sort_keys=True,
    ).encode("utf-8")
    payload = {
        "run_id": run_id,
        "source_snapshot_id": hashlib.sha256(snapshot_seed).hexdigest(),
        "captured_at": captured_at.isoformat(),
        "source_schema": schema,
        "source_connection": f"env:{args.dsn_env}",
        "source_write": False,
        "read_only_mode": True,
        "tables": table_manifest,
        "errors": errors,
        "status": "complete" if not errors else "partial_or_failed",
        "next_step": "profile table keys and map HD/XNY metadata concepts" if not errors else "repair failed table captures before profiling",
    }
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"run_id": run_id, "manifest": str(manifest_path), "status": payload["status"], "errors": len(errors)}, ensure_ascii=False))
    if errors:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
