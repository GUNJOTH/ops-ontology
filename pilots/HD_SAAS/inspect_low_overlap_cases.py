"""Inspect low-overlap device/location description cases without rewriting them."""
from __future__ import annotations

import csv
import json
import pathlib
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parent
INPUT = ROOT / "description_difference_analysis" / "description_difference_classification.csv"
LOCATIONS = ROOT / "context" / "locations.csv"
QUALITY = ROOT / "quality" / "hd_quality_candidates.csv"
OUTPUT_DIR = ROOT / "description_difference_analysis"
DETAILS = OUTPUT_DIR / "low_overlap_inspection.csv"
SUMMARY = OUTPUT_DIR / "low_overlap_inspection.json"
REPORT = ROOT / "reports" / "low_overlap_cases_20260812.md"

GENERIC_TERMS = {
    "系统", "设备", "部件", "电源", "开关", "柜", "箱", "阀", "门", "泵", "机", "号", "段", "备用",
    "控制", "手动", "电动", "气动", "自动", "母管", "入口", "出口", "一号", "二号", "三号", "四号",
}


def clean(value: object) -> str:
    return "" if value is None else str(value).strip()


def norm(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z\u3400-\u9fff]", "", value or "").lower()


def terms(value: str) -> set[str]:
    text = norm(value)
    result: set[str] = set()
    for size in (2, 3, 4):
        for index in range(0, max(0, len(text) - size + 1)):
            token = text[index : index + size]
            if any("\u4e00" <= char <= "\u9fff" for char in token) and token not in GENERIC_TERMS:
                result.add(token)
    return result


def main() -> None:
    location_lookup: dict[tuple[str, str], dict[str, str]] = {}
    with LOCATIONS.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            key = (clean(row.get("SITEID")), clean(row.get("LOCATION")))
            if key[0] and key[1]:
                location_lookup[key] = row

    rows: list[dict[str, str]] = []
    with INPUT.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            if clean(row.get("DIFFERENCE_CLASS")) == "LOW_TEXT_OVERLAP":
                rows.append(row)

    output_fields = [
        "SITEID", "ASSETNUM", "ASSET_DESCRIPTION", "LOCATION_CODE", "LOCATION_DESCRIPTION", "LOCATION_PARENT",
        "PARENT_DESCRIPTION", "LOCATION_SYSTEM", "LOCATION_TYPE", "TEXT_OVERLAP_SCORE", "SHARED_CONTENT_TERMS",
        "DEVICE_CONTEXT_HINT", "AUTOMATIC_INTERPRETATION", "REVIEW_STATUS", "CANDIDATE_ID", "SOURCE_ROW_HASH",
    ]
    category_counts: Counter[str] = Counter()
    site_counts: Counter[str] = Counter()
    system_counts: Counter[str] = Counter()
    type_counts: Counter[str] = Counter()
    parent_counts: Counter[str] = Counter()
    shared_term_counts: Counter[str] = Counter()
    examples: dict[str, list[dict[str, str]]] = defaultdict(list)

    with DETAILS.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=output_fields)
        writer.writeheader()
        for row in rows:
            site = clean(row.get("SITEID"))
            code = clean(row.get("LOCATION_CODE"))
            location = location_lookup.get((site, code), {})
            parent = clean(row.get("LOCATION_PARENT"))
            parent_row = location_lookup.get((site, parent), {}) if parent else {}
            asset_desc = clean(row.get("ASSET_DESCRIPTION"))
            location_desc = clean(row.get("LOCATION_DESCRIPTION"))
            shared = sorted(terms(asset_desc) & terms(location_desc), key=lambda item: (-len(item), item))
            system = clean(row.get("LOCATION_SYSTEM")) or clean(location.get("C_SYSTEM"))
            location_type = clean(row.get("LOCATION_TYPE")) or clean(location.get("TYPE"))
            # A transparent heuristic for inspection only; it does not change the candidate.
            asset_has_power = any(term in asset_desc for term in ("电源", "控制电源", "动力电源"))
            location_has_cabinet = any(term in location_desc for term in ("柜", "箱", "抽屉", "开关柜", "MCC", "PC段"))
            asset_has_specific_object = any(term in asset_desc for term in ("泵", "风机", "阀", "门", "变压器", "电机", "继电器", "模块", "秤"))
            location_is_power_point = any(term in location_desc for term in ("电源开关", "备用开关", "开关柜", "抽屉"))
            if asset_has_power and (location_has_cabinet or location_is_power_point):
                category = "DEVICE_VS_POWER_CABINET_LOCATION_LIKELY_VALID"
                interpretation = "设备描述可能是受电设备/控制对象，位置描述是电源柜、开关或抽屉位置"
            elif asset_has_specific_object and location_has_cabinet:
                category = "DEVICE_VS_CABINET_LOCATION_POSSIBLY_VALID"
                interpretation = "设备描述是具体设备，位置描述可能是安装柜/箱/间隔"
            elif shared:
                category = "LOW_OVERLAP_WITH_SHARED_DOMAIN_TERMS"
                interpretation = "文本低重合但存在设备领域词片段，需结合父级和系统判断"
            else:
                category = "LOW_OVERLAP_NO_STRONG_TEXT_EVIDENCE"
                interpretation = "设备描述与位置描述缺少直接文本证据，可能是位置关联或命名粒度差异"
            category_counts[category] += 1
            site_counts[site] += 1
            system_counts[system or "(空)"] += 1
            type_counts[location_type or "(空)"] += 1
            parent_counts[(parent_row.get("DESCRIPTION") or parent or "(空)")] += 1
            shared_term_counts.update(shared[:10])
            item = {
                "SITEID": site,
                "ASSETNUM": clean(row.get("ASSETNUM")),
                "ASSET_DESCRIPTION": asset_desc,
                "LOCATION_CODE": code,
                "LOCATION_DESCRIPTION": location_desc,
                "LOCATION_PARENT": parent,
                "PARENT_DESCRIPTION": clean(parent_row.get("DESCRIPTION")),
                "LOCATION_SYSTEM": system,
                "LOCATION_TYPE": location_type,
                "TEXT_OVERLAP_SCORE": clean(row.get("TEXT_OVERLAP_SCORE")),
                "SHARED_CONTENT_TERMS": ";".join(shared[:20]),
                "DEVICE_CONTEXT_HINT": category,
                "AUTOMATIC_INTERPRETATION": interpretation,
                "REVIEW_STATUS": "candidate_only",
                "CANDIDATE_ID": clean(row.get("CANDIDATE_ID")),
                "SOURCE_ROW_HASH": clean(row.get("SOURCE_ROW_HASH")),
            }
            writer.writerow(item)
            if len(examples[category]) < 8:
                examples[category].append(item)

    result = {
        "run_id": "hd-low-overlap-inspection-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
        "scope": "the 1,260 LOW_TEXT_OVERLAP cases in frozen HD quality batch",
        "rows_inspected": len(rows),
        "category_counts": dict(category_counts),
        "top_sites": dict(site_counts.most_common(20)),
        "top_systems": dict(system_counts.most_common(20)),
        "location_type_counts": dict(type_counts),
        "top_parent_descriptions": dict(parent_counts.most_common(20)),
        "top_shared_content_terms": dict(shared_term_counts.most_common(30)),
        "examples": examples,
        "outputs": [str(DETAILS)],
        "source_write": False,
        "formal_publication": False,
        "status": "inspection_only",
        "rule_version": "hd-low-overlap-inspection-0.1.0",
    }
    SUMMARY.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    report = [
        "# 1,260条低文本重合案例检查",
        "",
        "范围：冻结高质量批次中`ASSET.DESCRIPTION`与`LOCATIONS.DESCRIPTION`低文本重合的1,260条。",
        "",
        "## 自动观察分类",
        "",
    ]
    for category, count in category_counts.most_common():
        report.append(f"- `{category}`：{count}条")
    report.extend(
        [
            "",
            "分类只是解释性观察，不改写设备描述和位置描述：",
            "",
            "- `DEVICE_VS_POWER_CABINET_LOCATION_LIKELY_VALID`：设备描述与电源柜/开关/抽屉位置不同，可能是正常的设备—安装位置粒度差异。",
            "- `DEVICE_VS_CABINET_LOCATION_POSSIBLY_VALID`：设备描述是具体设备，位置描述可能是柜、箱或间隔。",
            "- `LOW_OVERLAP_WITH_SHARED_DOMAIN_TERMS`：仍有少量领域词关联，但不足以统一命名。",
            "- `LOW_OVERLAP_NO_STRONG_TEXT_EVIDENCE`：没有直接文本证据，暂不自动改写。",
            "",
            "## 示例",
            "",
        ]
    )
    for category, category_examples in examples.items():
        report.append(f"### {category}")
        report.append("")
        for item in category_examples[:3]:
            report.append(f"- `{item['ASSETNUM']}`：设备“{item['ASSET_DESCRIPTION']}”；位置“{item['LOCATION_DESCRIPTION']}”；父级“{item['PARENT_DESCRIPTION']}”；系统“{item['LOCATION_SYSTEM']}”。")
        report.append("")
    report.extend(
        [
            "完整逐条明细：[low_overlap_inspection.csv](../description_difference_analysis/low_overlap_inspection.csv)",
            "统计清单：[low_overlap_inspection.json](../description_difference_analysis/low_overlap_inspection.json)",
        ]
    )
    REPORT.write_text("\n".join(report) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
