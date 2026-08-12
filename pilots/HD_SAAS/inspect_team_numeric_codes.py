"""Read-only inspection of TEAMNUM/TEAM.DESCRIPTION and numeric business codes."""
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
dmdbms_bin = os.environ.get("DMDBMS_BIN")
if dmdbms_bin and pathlib.Path(dmdbms_bin).is_dir():
    os.add_dll_directory(dmdbms_bin)
import dmPython  # noqa: E402

OUTPUT = ROOT / "reports" / "numeric_code_identity_check_20260812.json"


def clean(value: object) -> str:
    return "" if value is None else str(value).strip()


def query_rows(cursor, sql: str) -> list[dict[str, str]]:
    cursor.execute(sql)
    names = [str(item[0]) for item in cursor.description]
    return [{name: clean(value) for name, value in zip(names, row)} for row in cursor.fetchall()]


def safe_query(cursor, sql: str) -> dict[str, object]:
    try:
        return {"status": "ok", "rows": query_rows(cursor, sql)}
    except Exception as exc:
        return {"status": "error", "error": str(exc), "rows": []}


def main() -> None:
    dsn = os.environ.get("HD_DM_DSN")
    if not dsn:
        raise SystemExit("请通过 HD_DM_DSN 环境变量提供 DM8 连接地址")
    connection = dmPython.connect(
        user=os.environ.get("HD_DM_USER", "HD_SAAS"),
        password=os.environ["HD_DM_PASSWORD"],
        dsn=dsn,
        schema="HD_SAAS",
        access_mode=dmPython.DSQL_MODE_READ_ONLY,
        autoCommit=True,
    )
    result: dict[str, object] = {
        "run_id": "hd-numeric-code-check-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
        "source_schema": "HD_SAAS",
        "source_host": dsn,
        "read_only": True,
        "source_write": False,
    }
    try:
        cursor = connection.cursor()
        result["team_tables"] = safe_query(
            cursor,
            "SELECT TABLE_NAME FROM USER_TABLES WHERE UPPER(TABLE_NAME) LIKE '%TEAM%' ORDER BY TABLE_NAME",
        )
        result["team_columns"] = safe_query(
            cursor,
            "SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE, DATA_LENGTH FROM USER_TAB_COLUMNS "
            "WHERE UPPER(TABLE_NAME) LIKE '%TEAM%' OR UPPER(COLUMN_NAME) LIKE '%TEAMNUM%' "
            "OR UPPER(COLUMN_NAME) LIKE '%TEAM%' ORDER BY TABLE_NAME, COLUMN_ID",
        )
        team_tables = {item["TABLE_NAME"] for item in result["team_tables"].get("rows", [])}  # type: ignore[union-attr]
        team_probe: dict[str, object] = {}
        for table in sorted(team_tables):
            columns = {item["COLUMN_NAME"] for item in result["team_columns"].get("rows", []) if item["TABLE_NAME"] == table}  # type: ignore[union-attr]
            team_probe[table] = {
                "columns": sorted(columns),
                "row_count": safe_query(cursor, f'SELECT COUNT(*) AS TOTAL_ROWS FROM "{table}"'),
                "sample": safe_query(cursor, f'SELECT * FROM "{table}" WHERE ROWNUM <= 10'),
            }
        result["team_probe"] = team_probe

        team_rows = query_rows(cursor, 'SELECT TEAMID, TEAMNUM, DESCRIPTION, STATUS, SITEID FROM "TEAM"')
        teamnum_values = [row["TEAMNUM"] for row in team_rows if row["TEAMNUM"]]
        team_description_values = [row["DESCRIPTION"] for row in team_rows if row["DESCRIPTION"]]
        numeric_teamnums = [value for value in teamnum_values if re.fullmatch(r"[0-9]+", value)]
        result["team_code_profile"] = {
            "rows": len(team_rows),
            "teamnum_nonempty": len(teamnum_values),
            "teamnum_distinct": len(set(teamnum_values)),
            "teamnum_numeric_only": len(numeric_teamnums),
            "teamnum_numeric_only_rate": len(numeric_teamnums) / len(teamnum_values) if teamnum_values else 0,
            "teamnum_alphanumeric": len([value for value in teamnum_values if re.search(r"[A-Za-z]", value) and re.search(r"[0-9]", value)]),
            "description_nonempty": len(team_description_values),
            "active_rows": len([row for row in team_rows if row["STATUS"] == "活动"]),
            "examples": team_rows[:20],
        }

        result["business_code_column_metadata"] = safe_query(
            cursor,
            "SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE, DATA_LENGTH FROM USER_TAB_COLUMNS "
            "WHERE UPPER(COLUMN_NAME) IN ('ASSETNUM','TEAMNUM','PERSONGROUP','LOCATION','WORKORDERNUM','WONUM') "
            "ORDER BY TABLE_NAME, COLUMN_ID",
        )
        result["asset_counts"] = safe_query(
            cursor,
            "SELECT COUNT(*) AS TOTAL_ROWS, COUNT(ASSETNUM) AS NONEMPTY_ROWS, "
            "COUNT(DISTINCT ASSETNUM) AS DISTINCT_VALUES FROM ASSET",
        )
        result["location_counts"] = safe_query(
            cursor,
            "SELECT COUNT(*) AS TOTAL_ROWS, COUNT(LOCATION) AS NONEMPTY_ROWS, "
            "COUNT(DISTINCT LOCATION) AS DISTINCT_VALUES FROM LOCATIONS",
        )
        cursor.close()
    finally:
        connection.close()
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
