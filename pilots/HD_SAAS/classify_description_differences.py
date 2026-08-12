"""Classify device/location description differences using deterministic evidence."""
from __future__ import annotations

import csv
import json
import pathlib
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parent
INPUT = ROOT / "kks_observed_patterns" / "high_quality_asset_location_relationships.csv"
OUTPUT_DIR = ROOT / "description_difference_analysis"
DETAILS = OUTPUT_DIR / "description_difference_classification.csv"
SUMMARY = OUTPUT_DIR / "manifest.json"
REPORT = ROOT / "reports" / "description_difference_analysis_20260812.md"


def clean(value: object) -> str:
    return "" if value is None else str(value).strip()


def normalize(value: str) -> str:
    # Keep Chinese/Latin/numeric content; punctuation and whitespace do not carry semantic identity here.
    return re.sub(r"[^0-9A-Za-z\u3400-\u9fff]", "", value or "").lower()


def ngrams(value: str, size: int = 2) -> set[str]:
    if not value:
        return set()
    if len(value) < size:
        return {value}
    return {value[index : index + size] for index in range(len(value) - size + 1)}


def classify(asset_description: str, location_description: str) -> tuple[str, float, str]:
    asset = normalize(asset_description)
    location = normalize(location_description)
    if not asset or not location:
        return "NOT_COMPARABLE", 0.0, "one_description_empty"
    if location in asset and asset != location:
        return "DEVICE_DESCRIPTION_CONTAINS_LOCATION", 1.0, "location_text_is_substring_of_asset_text"
    if asset in location and asset != location:
        return "LOCATION_DESCRIPTION_CONTAINS_DEVICE", 1.0, "asset_text_is_substring_of_location_text"
    asset_grams = ngrams(asset)
    location_grams = ngrams(location)
    union = asset_grams | location_grams
    score = len(asset_grams & location_grams) / len(union) if union else 0.0
    if score >= 0.50:
        return "HIGH_TEXT_OVERLAP", score, "shared_bigrams_at_or_above_50_percent"
    if score >= 0.20:
        return "PARTIAL_TEXT_OVERLAP", score, "shared_bigrams_between_20_and_50_percent"
    return "LOW_TEXT_OVERLAP", score, "shared_bigrams_below_20_percent"


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "SITEID", "ASSETNUM", "ASSET_DESCRIPTION", "LOCATION_CODE", "LOCATION_DESCRIPTION",
        "LOCATION_PARENT", "LOCATION_SYSTEM", "LOCATION_TYPE", "DIFFERENCE_CLASS",
        "TEXT_OVERLAP_SCORE", "EVIDENCE_REASON", "AUTO_ACTION", "REVIEW_STATUS",
        "CANDIDATE_ID", "SOURCE_ROW_HASH", "CONTEXT_STATUS",
    ]
    counts: Counter[str] = Counter()
    actions: Counter[str] = Counter()
    examples: dict[str, list[dict[str, str]]] = defaultdict(list)
    input_rows = 0
    difference_rows = 0
    with INPUT.open(encoding="utf-8-sig", newline="") as source, DETAILS.open("w", encoding="utf-8-sig", newline="") as output:
        reader = csv.DictReader(source)
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()
        for row in reader:
            input_rows += 1
            if clean(row.get("ASSET_LOCATION_DESCRIPTION_MATCH")) != "DIFFERENT_DESCRIPTIONS":
                continue
            difference_rows += 1
            difference_class, score, reason = classify(row.get("ASSET_DESCRIPTION", ""), row.get("LOCATION_DESCRIPTION", ""))
            if difference_class == "DEVICE_DESCRIPTION_CONTAINS_LOCATION":
                action = "KEEP_DEVICE_DESCRIPTION_AND_LOCATION_CONTEXT"
            elif difference_class == "LOCATION_DESCRIPTION_CONTAINS_DEVICE":
                action = "KEEP_DEVICE_DESCRIPTION_AND_LOCATION_CONTEXT"
            elif difference_class in {"HIGH_TEXT_OVERLAP", "PARTIAL_TEXT_OVERLAP"}:
                action = "KEEP_BOTH_DESCRIPTIONS_SEPARATELY"
            else:
                action = "DO_NOT_REWRITE_ROUTE_TO_REVIEW_LATER"
            counts[difference_class] += 1
            actions[action] += 1
            item = {
                "SITEID": clean(row.get("SITEID")),
                "ASSETNUM": clean(row.get("ASSETNUM")),
                "ASSET_DESCRIPTION": clean(row.get("ASSET_DESCRIPTION")),
                "LOCATION_CODE": clean(row.get("LOCATION_CODE")),
                "LOCATION_DESCRIPTION": clean(row.get("LOCATION_DESCRIPTION")),
                "LOCATION_PARENT": clean(row.get("LOCATION_PARENT")),
                "LOCATION_SYSTEM": clean(row.get("LOCATION_SYSTEM")),
                "LOCATION_TYPE": clean(row.get("LOCATION_TYPE")),
                "DIFFERENCE_CLASS": difference_class,
                "TEXT_OVERLAP_SCORE": f"{score:.4f}",
                "EVIDENCE_REASON": reason,
                "AUTO_ACTION": action,
                "REVIEW_STATUS": "candidate_only",
                "CANDIDATE_ID": clean(row.get("CANDIDATE_ID")),
                "SOURCE_ROW_HASH": clean(row.get("SOURCE_ROW_HASH")),
                "CONTEXT_STATUS": clean(row.get("CONTEXT_STATUS")),
            }
            writer.writerow(item)
            if len(examples[difference_class]) < 5:
                examples[difference_class].append(item)

    result = {
        "run_id": "hd-description-difference-analysis-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
        "scope": "frozen HD quality batch; only the 5,536 differing descriptions",
        "input_relationship_rows": input_rows,
        "differing_rows_classified": difference_rows,
        "classification_counts": dict(counts),
        "auto_action_counts": dict(actions),
        "classification_policy": {
            "DEVICE_DESCRIPTION_CONTAINS_LOCATION": "设备描述包含位置描述，保留设备描述为设备语义，位置描述为位置语义",
            "LOCATION_DESCRIPTION_CONTAINS_DEVICE": "位置描述包含设备描述，保留两者，不把位置扩写到设备名称",
            "HIGH_TEXT_OVERLAP": "文本高度重叠，保留两套字段，不改原文",
            "PARTIAL_TEXT_OVERLAP": "文本部分重叠，保留两套字段，不自动改写",
            "LOW_TEXT_OVERLAP": "证据关联弱，不自动改写",
        },
        "examples": examples,
        "source_write": False,
        "formal_publication": False,
        "status": "candidate_classification_only",
        "rule_version": "hd-description-difference-analysis-0.1.0",
        "outputs": [str(DETAILS)],
    }
    SUMMARY.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    report_lines = [
        "# 5,536条设备描述与功能位置描述差异自动辨别",
        "",
        "本分析只对高质量批次中设备描述与位置描述不一致的5,536条进行确定性分类。未改写原始描述，未写入源库。",
        "",
        "## 分类规则",
        "",
        "- 设备描述包含位置描述：设备描述通常是位置对象的设备化/部件化描述，保留设备描述，不覆盖位置描述。",
        "- 位置描述包含设备描述：位置描述更完整，设备名称仍保留ASSET.DESCRIPTION。",
        "- 高度/部分文本重叠：说明属于同一语义族，但不自动改名。",
        "- 低文本重叠：证据不足，不自动改写。",
        "",
        "## 结果",
        "",
    ]
    for category, count in counts.most_common():
        report_lines.append(f"- `{category}`：{count}条")
    report_lines.extend(
        [
            "",
            "自动动作只代表字段保留策略，不代表正式审批：",
            "",
            "- 可解释的包含/重叠关系：保留设备描述和位置描述两套字段；",
            "- 低重叠关系：不自动改写，标记为待后续处理；",
            "- 当前所有结果仍为candidate_only，未正式发布。",
            "",
            f"详细结果：[description_difference_classification.csv](../description_difference_analysis/description_difference_classification.csv)",
            f"运行清单：[manifest.json](../description_difference_analysis/manifest.json)",
        ]
    )
    REPORT.write_text("\n".join(report_lines) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
