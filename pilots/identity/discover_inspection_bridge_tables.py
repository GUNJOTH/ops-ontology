"""Discover possible inspection-to-device bridge tables from DM8 metadata only."""
from __future__ import annotations

import json
import os
import pathlib
import re
import sys
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parent
REPO_ROOT = ROOT.parents[2]
sys.path.insert(0, str(REPO_ROOT / "equipment-description-harness" / "tools" / "dmPython-src"))

TABLE_HINT = re.compile(r"(RELATION|RELATE|LINK|MAP|BRIDGE|CHECK|INSPECT|WORKORDER|ASSET|LOCATION|XJJL|INVENTORY|FAULT|TICKET)", re.I)
FIELD_HINT = re.compile(
    r"(ASSETNUM|ASSETID|KKS|EQUIPMENT|WONUM|WORKORDER|SOURCEASSET|TARGETASSET|SOURCELOCATION|TARGETLOCATION|"
    r"XJJLNUM|XJJLID|CHECKNUM|CHECKID|INVCHECKNUM|INVENTORYCHECK|ST_USECURITYCHECK|LOCATION|SITEID)",
    re.I,
)


def clean(value: object) -> str:
    return "" if value is None else str(value).strip()


def load_dm_python():
    dmdbms_bin = os.environ.get("DMDBMS_BIN")
    if not dmdbms_bin or not pathlib.Path(dmdbms_bin).is_dir():
        raise SystemExit("Missing DMDBMS_BIN")
    os.add_dll_directory(dmdbms_bin)
    import dmPython  # type: ignore
    return dmPython


def connect(dm_python, schema: str, dsn_env: str, user_env: str, password_env: str):
    return dm_python.connect(
        user=os.environ.get(user_env, schema),
        password=os.environ[password_env],
        dsn=os.environ[dsn_env],
        schema=schema,
        access_mode=dm_python.DSQL_MODE_READ_ONLY,
        autoCommit=True,
    )


def main() -> None:
    dm_python = load_dm_python()
    captured_at = datetime.now(timezone.utc)
    run_id = f"inspection-bridge-table-discovery-{captured_at.strftime('%Y%m%dT%H%M%SZ')}"
    output_dir = ROOT / "inventory" / run_id
    output_dir.mkdir(parents=True, exist_ok=False)
    schemas = (
        ("HD_SAAS", "HD_DM_DSN", "HD_DM_USER", "HD_DM_PASSWORD"),
        ("XNY_SAAS", "XNY_DM_DSN", "XNY_DM_USER", "XNY_DM_PASSWORD"),
    )
    records = []
    statuses = []
    for schema, dsn_env, user_env, password_env in schemas:
        connection = connect(dm_python, schema, dsn_env, user_env, password_env)
        try:
            cursor = connection.cursor()
            cursor.execute("SELECT TABLE_NAME, COLUMN_NAME FROM USER_TAB_COLUMNS ORDER BY TABLE_NAME, COLUMN_ID")
            grouped: dict[str, list[str]] = {}
            for table_name, column_name in cursor.fetchall():
                table = clean(table_name).upper()
                column = clean(column_name).upper()
                grouped.setdefault(table, []).append(column)
            cursor.close()
            for table, columns in grouped.items():
                table_match = bool(TABLE_HINT.search(table))
                relevant = [column for column in columns if FIELD_HINT.search(column)]
                if table_match or relevant:
                    records.append({
                        "schema": schema,
                        "table": table,
                        "table_name_hint": table_match,
                        "relevant_columns": relevant,
                        "all_columns": columns,
                    })
            statuses.append({"schema": schema, "status": "ok", "table_count": len(grouped)})
        finally:
            connection.close()
    report = {
        "run_id": run_id,
        "captured_at": captured_at.isoformat(),
        "schemas": statuses,
        "records": records,
        "read_only": True,
        "source_write": False,
        "formal_publication": False,
        "policy": "metadata only; no business rows queried",
    }
    path = output_dir / "bridge-table-discovery.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"run_id": run_id, "record_count": len(records), "output": str(path), "read_only": True}, ensure_ascii=False))


if __name__ == "__main__":
    main()
