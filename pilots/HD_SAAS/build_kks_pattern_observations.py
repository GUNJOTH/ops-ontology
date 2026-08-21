"""Observe KKS code patterns from parent/system/name evidence without formal decoding."""
from __future__ import annotations

import csv
import json
import os
import pathlib
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parent
REPO_ROOT = ROOT.parents[2]
sys.path.insert(0, str(REPO_ROOT / "equipment-description-harness" / "tools" / "dmPython-src"))
dmdbms_bin = os.environ.get("DMDBMS_BIN")
if dmdbms_bin and pathlib.Path(dmdbms_bin).is_dir():
    os.add_dll_directory(dmdbms_bin)
import dmPython  # noqa: E402

DICTIONARY = ROOT / "kks_dictionary" / "kks_code_meaning_dictionary.csv"
OUTPUT_DIR = ROOT / "kks_observed_patterns"
SUMMARY = OUTPUT_DIR / "source_observation_summary.csv"
TOKEN_OUTPUT = OUTPUT_DIR / "token_observations.csv"
SHAPE_OUTPUT = OUTPUT_DIR / "code_shape_observations.csv"
MANIFEST = OUTPUT_DIR / "manifest.json"
REPORT = ROOT / "reports" / "kks_pattern_observations_20260812.md"

MAPPING_TABLES = [
    "LOCATIONS_TEMP10",
    "LOCATIONS_TEMP11",
    "LOCATIONS_TEMP15",
    "LOCATIONS_TEMP16",
    "LOCATIONS_TEMP20",
    "LOCATIONS_TEMPMMJ",
    "LOCATIONS_TEMPNJHX",
    "LOCATIONS_TEMPXZ",
    "IMP_DATA_LOCATIONS",
]

FIELD_ALIASES = {
    "code": ["KKS", "KKS编码", "LOCATION", "KKSLOCATION"],
    "parent": ["PARENT", "父级KKS编码", "FJKKS", "PARENTKKS"],
    "name": ["NAME", "设备名称", "SBMC", "名称"],
    "system": ["C_SYSTEM", "系统", "XT"],
    "node_type": ["C_SBLX", "节点类型", "SBLX"],
    "profession": ["PROFESS", "专业", "ZY"],
    "unit": ["C_JZH", "机组号", "JZH"],
    "location": ["地点", "AZWZ"],
    "model": ["MODEL", "规格型号", "GGXH"],
}


def clean(value: object) -> str:
    return "" if value is None else str(value).strip()


def to_int(value: object, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def quote_ident(value: object) -> str:
    return '"' + str(value).replace('"', '""') + '"'


def pick(row: dict[str, str], aliases: list[str]) -> str:
    for alias in aliases:
        if clean(row.get(alias)):
            return clean(row[alias])
    return ""


def code_shape(code: str) -> str:
    return "".join("9" if char.isdigit() else "A" if char.isalpha() else "X" for char in code.upper())


def tokens(code: str) -> list[tuple[str, int, int]]:
    return [(match.group(0), match.start(), len(match.group(0))) for match in re.finditer(r"[A-Z]+", code.upper())]


def add_example(bucket: list[str], value: str, limit: int = 5) -> None:
    if value and value not in bucket and len(bucket) < limit:
        bucket.append(value)


def new_stat() -> dict[str, object]:
    return {
        "rows": 0,
        "codes": set(),
        "parent_nonempty": 0,
        "parent_prefix_match": 0,
        "name_nonempty": 0,
        "system_nonempty": 0,
        "node_type_nonempty": 0,
        "token_counts": Counter(),
        "shape_counts": Counter(),
    }


def observe_row(
    source_table: str,
    row: dict[str, str],
    source_stats: dict[str, dict[str, object]],
    token_stats: dict[tuple[str, int, int], dict[str, object]],
    shape_stats: dict[tuple[str, str], dict[str, object]],
) -> None:
    code = pick(row, FIELD_ALIASES["code"]).upper()
    if not code or not re.fullmatch(r"[A-Z0-9][A-Z0-9._/-]*", code):
        return
    parent = pick(row, FIELD_ALIASES["parent"]).upper()
    name = pick(row, FIELD_ALIASES["name"])
    system = pick(row, FIELD_ALIASES["system"])
    node_type = pick(row, FIELD_ALIASES["node_type"])
    stat = source_stats[source_table]
    stat["rows"] += 1
    stat["codes"].add(code)
    if parent:
        stat["parent_nonempty"] += 1
    if parent and code.startswith(parent):
        stat["parent_prefix_match"] += 1
    if name:
        stat["name_nonempty"] += 1
    if system:
        stat["system_nonempty"] += 1
    if node_type:
        stat["node_type_nonempty"] += 1

    shape_key = (source_table, code_shape(code))
    shape = shape_stats.setdefault(
        shape_key,
        {"count": 0, "codes": set(), "parent_prefix_match": 0, "examples": []},
    )
    shape["count"] += 1
    shape["codes"].add(code)
    if parent and code.startswith(parent):
        shape["parent_prefix_match"] += 1
    add_example(shape["examples"], code)

    for token, offset, token_len in tokens(code):
        key = (token, offset, token_len)
        item = token_stats.setdefault(
            key,
            {
                "occurrence_count": 0,
                "codes": set(),
                "parent_prefix_match": 0,
                "systems": Counter(),
                "names": Counter(),
                "node_types": Counter(),
                "examples": [],
                "source_tables": Counter(),
            },
        )
        item["occurrence_count"] += 1
        item["codes"].add(code)
        item["source_tables"][source_table] += 1
        if parent and code.startswith(parent):
            item["parent_prefix_match"] += 1
        if system:
            item["systems"][system] += 1
        if name:
            item["names"][name] += 1
        if node_type:
            item["node_types"][node_type] += 1
        add_example(item["examples"], code)
        stat["token_counts"][token] += 1


def connect():
    dsn = os.environ.get("HD_DM_DSN")
    if not dsn:
        raise SystemExit("请通过 HD_DM_DSN 环境变量提供 DM8 连接地址")
    return dmPython.connect(
        user=os.environ.get("HD_DM_USER", "HD_SAAS"),
        password=os.environ["HD_DM_PASSWORD"],
        dsn=dsn,
        schema="HD_SAAS",
        access_mode=dmPython.DSQL_MODE_READ_ONLY,
        autoCommit=True,
    )


def main() -> None:
    dsn = os.environ.get("HD_DM_DSN")
    if not dsn:
        raise SystemExit("请通过 HD_DM_DSN 环境变量提供 DM8 连接地址")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    source_stats: dict[str, dict[str, object]] = defaultdict(new_stat)
    token_stats: dict[tuple[str, int, int], dict[str, object]] = {}
    shape_stats: dict[tuple[str, str], dict[str, object]] = {}

    # Current high-quality code dictionary is already a local, frozen-batch artifact.
    with DICTIONARY.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            observe_row("HIGH_QUALITY_DICTIONARY", row, source_stats, token_stats, shape_stats)

    connection = connect()
    try:
        cursor = connection.cursor()
        for table in MAPPING_TABLES:
            cursor.execute(f'SELECT * FROM {quote_ident(table)}')
            names = [str(item[0]) for item in cursor.description]
            while True:
                batch = cursor.fetchmany(5000)
                if not batch:
                    break
                for values in batch:
                    row = {name: clean(value) for name, value in zip(names, values)}
                    observe_row(table, row, source_stats, token_stats, shape_stats)
        cursor.close()
    finally:
        connection.close()

    summary_fields = [
        "SOURCE_TABLE", "OBSERVED_ROWS", "DISTINCT_CODES", "PARENT_NONEMPTY_ROWS",
        "PARENT_PREFIX_MATCH_ROWS", "PARENT_PREFIX_MATCH_RATE", "NAME_NONEMPTY_ROWS",
        "SYSTEM_NONEMPTY_ROWS", "NODE_TYPE_NONEMPTY_ROWS", "TOP_CODE_SHAPES", "TOP_TOKENS",
        "OBSERVATION_STATUS",
    ]
    with SUMMARY.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=summary_fields)
        writer.writeheader()
        for table, stat in sorted(source_stats.items()):
            rows = to_int(stat["rows"])
            prefix_matches = to_int(stat["parent_prefix_match"])
            parent_nonempty = to_int(stat["parent_nonempty"])
            top_shapes = "; ".join(f"{shape} ({count})" for shape, count in stat["shape_counts"].most_common(5))
            top_tokens = "; ".join(f"{token} ({count})" for token, count in stat["token_counts"].most_common(10))
            writer.writerow(
                {
                    "SOURCE_TABLE": table,
                    "OBSERVED_ROWS": rows,
                    "DISTINCT_CODES": len(stat["codes"]),
                    "PARENT_NONEMPTY_ROWS": stat["parent_nonempty"],
                    "PARENT_PREFIX_MATCH_ROWS": prefix_matches,
                    "PARENT_PREFIX_MATCH_RATE": f"{prefix_matches / parent_nonempty:.4f}" if parent_nonempty else "",
                    "NAME_NONEMPTY_ROWS": stat["name_nonempty"],
                    "SYSTEM_NONEMPTY_ROWS": stat["system_nonempty"],
                    "NODE_TYPE_NONEMPTY_ROWS": stat["node_type_nonempty"],
                    "TOP_CODE_SHAPES": top_shapes,
                    "TOP_TOKENS": top_tokens,
                    "OBSERVATION_STATUS": "OBSERVED_ASSOCIATION_ONLY",
                }
            )

    token_fields = [
        "TOKEN", "CODE_POSITION", "TOKEN_LENGTH", "OCCURRENCE_COUNT", "DISTINCT_CODE_COUNT",
        "PARENT_PREFIX_MATCH_COUNT", "PARENT_PREFIX_MATCH_RATE", "TOP_SYSTEMS", "TOP_DEVICE_NAMES",
        "TOP_NODE_TYPES", "SOURCE_TABLES", "CODE_EXAMPLES", "SEMANTIC_STATUS",
    ]
    with TOKEN_OUTPUT.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=token_fields)
        writer.writeheader()
        for (token, offset, token_len), item in sorted(token_stats.items(), key=lambda pair: (-pair[1]["occurrence_count"], pair[0])):
            count = to_int(item["occurrence_count"])
            match_count = to_int(item["parent_prefix_match"])
            top_systems = "; ".join(f"{value} ({count_})" for value, count_ in item["systems"].most_common(5))
            top_names = "; ".join(f"{value} ({count_})" for value, count_ in item["names"].most_common(5))
            top_types = "; ".join(f"{value} ({count_})" for value, count_ in item["node_types"].most_common(5))
            source_tables = "; ".join(f"{value} ({count_})" for value, count_ in item["source_tables"].most_common())
            stable_context = bool(item["systems"] or item["names"] or item["node_types"])
            writer.writerow(
                {
                    "TOKEN": token,
                    "CODE_POSITION": offset,
                    "TOKEN_LENGTH": token_len,
                    "OCCURRENCE_COUNT": count,
                    "DISTINCT_CODE_COUNT": len(item["codes"]),
                    "PARENT_PREFIX_MATCH_COUNT": match_count,
                    "PARENT_PREFIX_MATCH_RATE": f"{match_count / count:.4f}" if count else "",
                    "TOP_SYSTEMS": top_systems,
                    "TOP_DEVICE_NAMES": top_names,
                    "TOP_NODE_TYPES": top_types,
                    "SOURCE_TABLES": source_tables,
                    "CODE_EXAMPLES": "; ".join(item["examples"]),
                    "SEMANTIC_STATUS": "OBSERVED_CONTEXT_WITHOUT_FORMAL_KEY_PART_MEANING" if stable_context else "NO_CONTEXT_EVIDENCE",
                }
            )

    shape_fields = ["SOURCE_TABLE", "CODE_SHAPE", "OCCURRENCE_COUNT", "DISTINCT_CODE_COUNT", "PARENT_PREFIX_MATCH_COUNT", "EXAMPLES", "OBSERVATION_STATUS"]
    with SHAPE_OUTPUT.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=shape_fields)
        writer.writeheader()
        for (table, shape), item in sorted(shape_stats.items(), key=lambda pair: (-pair[1]["count"], pair[0])):
            writer.writerow(
                {
                    "SOURCE_TABLE": table,
                    "CODE_SHAPE": shape,
                    "OCCURRENCE_COUNT": item["count"],
                    "DISTINCT_CODE_COUNT": len(item["codes"]),
                    "PARENT_PREFIX_MATCH_COUNT": item["parent_prefix_match"],
                    "EXAMPLES": "; ".join(item["examples"]),
                    "OBSERVATION_STATUS": "STRUCTURAL_PATTERN_ONLY",
                }
            )

    manifest = {
        "run_id": "hd-kks-observed-patterns-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
        "source_schema": "HD_SAAS",
        "source_host": dsn,
        "scope": "frozen high-quality dictionary plus read-only KKS mapping candidates",
        "source_tables": ["LOCATIONS", *MAPPING_TABLES],
        "current_quality_dictionary_rows": sum(1 for _ in csv.DictReader(DICTIONARY.open(encoding="utf-8-sig", newline=""))),
        "mapping_tables": MAPPING_TABLES,
        "output_files": [str(SUMMARY), str(TOKEN_OUTPUT), str(SHAPE_OUTPUT)],
        "formal_kks_decoding": False,
        "semantic_assignment": False,
        "source_write": False,
        "status": "observed_patterns_only",
        "rule_version": "hd-kks-observed-patterns-0.1.0",
    }
    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    report_lines = [
        "# HD_SAAS KKS编码规律观察",
        "",
        f"- run_id: `{manifest['run_id']}`",
        "- 范围：冻结高质量编码字典 + KKS整码映射候选表",
        "- 方式：父级前缀、编码形态、字母片段、系统、设备名称、节点类型联合观察",
        "- 结论状态：仅观察性证据，不发布正式KKS位段含义",
        "- 源库写入：否",
        "",
        "## 输出",
        "",
        f"- [来源汇总](../kks_observed_patterns/source_observation_summary.csv)",
        f"- [编码片段观察](../kks_observed_patterns/token_observations.csv)",
        f"- [编码形态观察](../kks_observed_patterns/code_shape_observations.csv)",
        f"- [运行清单](../kks_observed_patterns/manifest.json)",
        "",
        "## 解释边界",
        "",
        "`TOKEN`表示编码中重复出现的字母片段，`TOP_SYSTEMS`、`TOP_DEVICE_NAMES`和`TOP_NODE_TYPES`只是该片段的观测上下文；它们不等于正式KKS Key Part翻译。",
        "只有在有电厂位段目录或明确的版本化规则后，才可把观察结果升级为正式语义规则。",
    ]
    REPORT.write_text("\n".join(report_lines) + "\n", encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
