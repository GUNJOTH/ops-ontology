"""Read-only check for KKS key-part/catalog tables and fields in HD_SAAS."""
from __future__ import annotations

import json
import os
import pathlib
import sys
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parent
REPO_ROOT = ROOT.parents[2]
sys.path.insert(0, str(REPO_ROOT / "equipment-description-harness" / "tools" / "dmPython-src"))
dmdbms_bin = os.environ.get("DMDBMS_BIN")
if dmdbms_bin and pathlib.Path(dmdbms_bin).is_dir():
    os.add_dll_directory(dmdbms_bin)
import dmPython  # noqa: E402

OUTPUT_JSON = ROOT / "reports" / "kks_catalog_check_20260812.json"
OUTPUT_MD = ROOT / "reports" / "kks_catalog_check_20260812.md"


def clean(value: object) -> str:
    return "" if value is None else str(value).strip()


def rows(cursor, sql: str) -> list[dict[str, str]]:
    cursor.execute(sql)
    names = [str(item[0]) for item in cursor.description]
    return [{name: clean(value) for name, value in zip(names, row)} for row in cursor.fetchall()]


def safe_rows(cursor, sql: str) -> dict[str, object]:
    try:
        return {"status": "ok", "rows": rows(cursor, sql)}
    except Exception as exc:  # preserve diagnostic without credentials
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
        "run_id": "hd-kks-catalog-check-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
        "source_schema": "HD_SAAS",
        "source_host": dsn,
        "read_only": True,
        "source_write": False,
    }
    try:
        cursor = connection.cursor()
        result["kks_named_tables"] = safe_rows(
            cursor,
            "SELECT TABLE_NAME FROM USER_TABLES "
            "WHERE UPPER(TABLE_NAME) LIKE '%KKS%' OR UPPER(TABLE_NAME) LIKE '%KEYPART%' "
            "ORDER BY TABLE_NAME",
        )
        result["kks_named_columns"] = safe_rows(
            cursor,
            "SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE, DATA_LENGTH "
            "FROM USER_TAB_COLUMNS WHERE UPPER(COLUMN_NAME) LIKE '%KKS%' "
            "OR UPPER(COLUMN_NAME) LIKE '%KEYPART%' OR UPPER(COLUMN_NAME) LIKE '%OLDLOCATION%' "
            "ORDER BY TABLE_NAME, COLUMN_ID",
        )
        result["relevant_columns"] = safe_rows(
            cursor,
            "SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE, DATA_LENGTH "
            "FROM USER_TAB_COLUMNS WHERE TABLE_NAME IN "
            "('ASSET','LOCATIONS','LOCHIERARCHY','GRPATTRIBUTE') "
            "AND (UPPER(COLUMN_NAME) LIKE '%LOCATION%' OR UPPER(COLUMN_NAME) LIKE '%SYSTEM%' "
            "OR UPPER(COLUMN_NAME) LIKE '%JZH%' OR UPPER(COLUMN_NAME) LIKE '%SBLX%') "
            "ORDER BY TABLE_NAME, COLUMN_ID",
        )
        result["grpattribute_kks_metadata"] = safe_rows(
            cursor,
            "SELECT OBJECTNAME, ATTRIBUTENAME, COLUMNNAME, TITLE, REMARKS, DOMAINID, TYPE, LENGTH "
            "FROM GRPATTRIBUTE WHERE UPPER(ATTRIBUTENAME) LIKE '%KKS%' "
            "OR UPPER(COLUMNNAME) LIKE '%KKS%' OR UPPER(ATTRIBUTENAME) LIKE '%OLDLOCATION%' "
            "OR UPPER(COLUMNNAME) LIKE '%OLDLOCATION%' OR UPPER(TITLE) LIKE '%KKS%' "
            "OR UPPER(REMARKS) LIKE '%KKS%' ORDER BY OBJECTNAME, ATTRIBUTENAME",
        )

        # Only query a field after metadata confirms that it exists.
        column_result = result["relevant_columns"]
        existing = {
            (item["TABLE_NAME"], item["COLUMN_NAME"])
            for item in column_result.get("rows", [])  # type: ignore[union-attr]
        }
        probe_fields = {
            "ASSET": ["C_OLDLOCATION", "LOCATION", "C_JZH", "C_SBLX"],
            "LOCATIONS": ["C_OLDLOCATION", "LOCATION", "LOCATIONSCODE", "C_SYSTEM", "C_JZH", "C_SBLX"],
        }
        probes: dict[str, object] = {}
        for table, fields in probe_fields.items():
            for field in fields:
                if (table, field) not in existing:
                    continue
                alias = f"{table}.{field}"
                probes[alias] = safe_rows(
                    cursor,
                    f'SELECT COUNT(*) AS TOTAL_ROWS, COUNT("{field}") AS NONEMPTY_ROWS, '
                    f'COUNT(DISTINCT "{field}") AS DISTINCT_VALUES FROM {table}',
                )
                probes[alias + ".examples"] = safe_rows(
                    cursor,
                    f'SELECT "{field}" AS VALUE FROM {table} '
                    f'WHERE "{field}" IS NOT NULL AND ROWNUM <= 20',
                )
        result["field_value_probes"] = probes

        candidate_table_names = {
            item["TABLE_NAME"]
            for item in result["kks_named_columns"].get("rows", [])  # type: ignore[union-attr]
            if any(token in item["TABLE_NAME"] for token in ("KKS", "LOCATION", "ST_PI", "SYSSBLIST", "TZ_CZDATA", "WORK", "JSJDLINE", "C_DFINFO", "C_FAULT"))
        }
        table_counts: dict[str, object] = {}
        for table in sorted(candidate_table_names):
            table_counts[table] = safe_rows(cursor, f'SELECT COUNT(*) AS TOTAL_ROWS FROM "{table}"')
        result["candidate_table_counts"] = table_counts

        sample_tables = [
            "LOCATIONS_TEMP",
            "LOCATIONS_TEMP10",
            "LOCATIONS_TEMP11",
            "LOCATIONS_TEMP15",
            "LOCATIONS_TEMP16",
            "LOCATIONS_TEMP20",
            "LOCATIONS_TEMPMMJ",
            "LOCATIONS_TEMPNJHX",
            "LOCATIONS_TEMPXZ",
            "LOCATIONTEMP_20231109",
            "LOCATION_TEMP3",
            "IMP_DATA_LOCATIONS",
            "KKSAPPLY",
            "KKSAPPLYLINE",
            "TZ_CZDATA",
        ]
        candidate_samples: dict[str, object] = {}
        for table in sample_tables:
            columns = safe_rows(
                cursor,
                f"SELECT COLUMN_ID, COLUMN_NAME, DATA_TYPE, DATA_LENGTH FROM USER_TAB_COLUMNS "
                f"WHERE TABLE_NAME = '{table}' ORDER BY COLUMN_ID",
            )
            candidate_samples[table] = {
                "columns": columns,
                "sample_rows": safe_rows(cursor, f'SELECT * FROM "{table}" WHERE ROWNUM <= 5'),
            }
        result["candidate_table_samples"] = candidate_samples
        cursor.close()
    finally:
        connection.close()

    OUTPUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_JSON.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [
        "# HD_SAAS KKS位段目录/字段只读核查",
        "",
        f"- run_id: `{result['run_id']}`",
        f"- source: `HD_SAAS@{result['source_host']}`",
        "- source_write: `false`",
        "",
        "## 结论依据",
        "",
        "- `USER_TABLES` 中按 KKS/KEYPART 名称检索的结果见 JSON。",
        "- `USER_TAB_COLUMNS` 中按 KKS/KEYPART/OLDLOCATION 检索的结果见 JSON。",
        "- `GRPATTRIBUTE` 命中的字段定义见 JSON；元数据定义不等于业务数据已填值。",
        "- `C_OLDLOCATION` 若存在，按元数据含义作为“原KKS编码”候选，不直接当作正式位段目录。",
        "- `LOCATIONSCODE`、`C_SYSTEM`、`C_JZH`、`C_SBLX` 仅作为位置/系统/机组/设备类型上下文，需结合实际非空值判断。",
        "",
        "完整结果：[kks_catalog_check_20260812.json](kks_catalog_check_20260812.json)",
    ]
    OUTPUT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
