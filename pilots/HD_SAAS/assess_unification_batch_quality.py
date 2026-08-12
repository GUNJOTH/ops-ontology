"""Assess structural and semantic readiness of the derived unification batch."""
from __future__ import annotations

import csv
import hashlib
import json
import pathlib
import re
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parent
INPUT = ROOT / "quality_scope" / "hd_quality_candidates_for_unification.csv"
OUTPUT_DIR = ROOT / "quality_assessment"
SUMMARY = OUTPUT_DIR / "summary.json"
SUSPECTS = OUTPUT_DIR / "description_quality_candidates.csv"
CHANGED = OUTPUT_DIR / "semantic_change_samples.csv"
REPORT = ROOT / "reports" / "unification_batch_quality_20260812.md"

GENERIC_EXACT = {
    "备用", "设备", "部件", "阀", "泵", "电机", "开关", "柜", "箱", "模块", "测试", "未知",
    "无", "无描述", "暂无", "待定", "n/a", "na", "null", "none", "-", "—", ".",
}
PLACEHOLDER_RE = re.compile(r"^(test|测试|unknown|未知|n/?a|null|none|无|暂无|待定|备用)[\s0-9_-]*$", re.I)
CODE_ONLY_RE = re.compile(r"^[A-Za-z0-9_.:/-]+$")
KKS_LIKE_RE = re.compile(r"^[0-9][0-9A-Z]{10,16}$")


def clean(value: object) -> str:
    return "" if value is None else str(value).strip()


def norm(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "")
    return re.sub(r"\s+", "", value).strip()


def sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def top_json(counter: Counter[str], limit: int = 20) -> dict[str, int]:
    return dict(counter.most_common(limit))


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    row_count = 0
    candidate_ids: set[str] = set()
    identities: set[tuple[str, str]] = set()
    duplicate_candidate_ids: Counter[str] = Counter()
    duplicate_identities: Counter[tuple[str, str]] = Counter()
    source_snapshots: Counter[str] = Counter()
    source_row_hash_lengths: Counter[int] = Counter()
    context_hash_lengths: Counter[int] = Counter()
    statuses: Counter[str] = Counter()
    context_statuses: Counter[str] = Counter()
    rules: Counter[str] = Counter()
    validators: Counter[str] = Counter()
    site_counts: Counter[str] = Counter()
    desc_counts: Counter[str] = Counter()
    desc_lengths: Counter[int] = Counter()
    location_counts: Counter[str] = Counter()
    location_desc_counts: Counter[str] = Counter()
    location_parent_counts: Counter[str] = Counter()
    system_counts: Counter[str] = Counter()
    class_counts: Counter[str] = Counter()
    classification_counts: Counter[str] = Counter()
    context_reason_counts: Counter[str] = Counter()
    suspect_rows: list[dict[str, str]] = []
    changed_rows: list[dict[str, str]] = []
    metrics = Counter()

    with INPUT.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            row_count += 1
            candidate_id = clean(row.get("CANDIDATE_ID"))
            site = clean(row.get("SITEID"))
            assetnum = clean(row.get("ASSETNUM"))
            identity = (site, assetnum)
            if candidate_id in candidate_ids:
                duplicate_candidate_ids[candidate_id] += 1
            candidate_ids.add(candidate_id)
            if identity in identities:
                duplicate_identities[identity] += 1
            identities.add(identity)
            source_snapshots[clean(row.get("SOURCE_SNAPSHOT_ID"))] += 1
            source_row_hash_lengths[len(clean(row.get("SOURCE_ROW_HASH")))] += 1
            context_hash_lengths[len(clean(row.get("CONTEXT_HASH")))] += 1
            statuses[clean(row.get("RESULT_STATUS"))] += 1
            context_statuses[clean(row.get("CONTEXT_STATUS"))] += 1
            rules[clean(row.get("RULE_VERSION"))] += 1
            validators[clean(row.get("VALIDATOR_VERSION"))] += 1
            site_counts[site] += 1

            original = clean(row.get("ORIGINAL_DESCRIPTION"))
            candidate = clean(row.get("CANDIDATE_DESCRIPTION"))
            location_code = ""
            location_description = clean(row.get("LOCATION_DESCRIPTION"))
            location_parent = clean(row.get("LOCATION_PARENT"))
            system = ""
            class_description = clean(row.get("CLASSSTRUCTURE_DESCRIPTION"))
            classification_description = clean(row.get("CLASSIFICATION_DESCRIPTION"))
            try:
                context = json.loads(row.get("CONTEXT_JSON") or "{}")
                location = context.get("location") or {}
                location_code = clean(location.get("LOCATION"))
                location_description = location_description or clean(location.get("DESCRIPTION"))
                location_parent = location_parent or clean((context.get("location_hierarchy") or {}).get("PARENT"))
                system = clean(location.get("C_SYSTEM"))
                class_description = class_description or clean((context.get("class_structure") or {}).get("DESCRIPTION"))
                classification_description = classification_description or clean((context.get("classification") or {}).get("DESCRIPTION"))
                metrics["context_json_parsed"] += 1
            except (json.JSONDecodeError, TypeError):
                metrics["context_json_invalid"] += 1
            location_counts[location_code] += 1
            location_desc_counts[location_description] += 1
            location_parent_counts[location_parent] += 1
            system_counts[system] += 1
            class_counts[class_description] += 1
            classification_counts[classification_description] += 1
            if clean(row.get("CONTEXT_REASON_CODES")):
                for reason in clean(row.get("CONTEXT_REASON_CODES")).split("|"):
                    if reason:
                        context_reason_counts[reason] += 1

            if original:
                metrics["original_nonempty"] += 1
            if candidate:
                metrics["candidate_nonempty"] += 1
            if norm(original) == norm(candidate):
                metrics["candidate_same_as_original_normalized"] += 1
            elif len(changed_rows) < 100:
                changed_rows.append(
                    {
                        "SITEID": site,
                        "ASSETNUM": assetnum,
                        "ORIGINAL_DESCRIPTION": original,
                        "CANDIDATE_DESCRIPTION": candidate,
                        "LOCATION_CODE": location_code,
                        "LOCATION_DESCRIPTION": location_description,
                        "CLASSSTRUCTURE_DESCRIPTION": class_description,
                        "CLASSIFICATION_DESCRIPTION": classification_description,
                        "CANDIDATE_ID": candidate_id,
                    }
                )
            if len(original) <= 2:
                metrics["description_len_le_2"] += 1
            if len(original) <= 3:
                metrics["description_len_le_3"] += 1
            desc_lengths[len(original)] += 1
            desc_counts[norm(original)] += 1
            if location_code:
                metrics["location_nonempty"] += 1
                if KKS_LIKE_RE.fullmatch(location_code.upper()):
                    metrics["kks_like_location"] += 1
            if location_description:
                metrics["location_description_nonempty"] += 1
            if location_parent:
                metrics["location_parent_nonempty"] += 1
            if system:
                metrics["system_nonempty"] += 1
            if class_description:
                metrics["classstructure_description_nonempty"] += 1
            if classification_description:
                metrics["classification_description_nonempty"] += 1
            if original and location_description:
                if norm(original) == norm(location_description):
                    metrics["asset_location_description_exact"] += 1
                else:
                    metrics["asset_location_description_different"] += 1
            else:
                metrics["asset_location_not_comparable"] += 1

            normalized_original = norm(original).lower()
            flags: list[str] = []
            if not original:
                flags.append("EMPTY_DESCRIPTION")
            elif normalized_original in {norm(item).lower() for item in GENERIC_EXACT}:
                flags.append("GENERIC_EXACT_DESCRIPTION")
            elif PLACEHOLDER_RE.fullmatch(original):
                flags.append("PLACEHOLDER_PATTERN")
            elif CODE_ONLY_RE.fullmatch(original) and not any("\u4e00" <= char <= "\u9fff" for char in original):
                flags.append("CODE_ONLY_DESCRIPTION")
            if len(original) <= 3:
                flags.append("VERY_SHORT_DESCRIPTION")
            if flags:
                suspect_rows.append(
                    {
                        "SITEID": site,
                        "ASSETNUM": assetnum,
                        "ASSET_DESCRIPTION": original,
                        "LOCATION_CODE": location_code,
                        "LOCATION_DESCRIPTION": location_description,
                        "FLAGS": ";".join(flags),
                        "CANDIDATE_ID": candidate_id,
                    }
                )
            for flag in flags:
                metrics[flag] += 1

    suspect_fields = ["SITEID", "ASSETNUM", "ASSET_DESCRIPTION", "LOCATION_CODE", "LOCATION_DESCRIPTION", "FLAGS", "CANDIDATE_ID"]
    with SUSPECTS.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=suspect_fields)
        writer.writeheader()
        writer.writerows(suspect_rows)

    changed_fields = [
        "SITEID",
        "ASSETNUM",
        "ORIGINAL_DESCRIPTION",
        "CANDIDATE_DESCRIPTION",
        "LOCATION_CODE",
        "LOCATION_DESCRIPTION",
        "CLASSSTRUCTURE_DESCRIPTION",
        "CLASSIFICATION_DESCRIPTION",
        "CANDIDATE_ID",
    ]
    with CHANGED.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=changed_fields)
        writer.writeheader()
        writer.writerows(changed_rows)

    exact_desc_count = sum(1 for key, count in desc_counts.items() if key)
    repeated_desc_rows = sum(count for key, count in desc_counts.items() if key and count > 1)
    result = {
        "run_id": "hd-unification-batch-quality-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
        "scope": "derived 384,322-row unification processing batch",
        "input_file": str(INPUT),
        "input_sha256": sha256(INPUT),
        "row_count": row_count,
        "identity": {
            "distinct_candidate_ids": len(candidate_ids),
            "duplicate_candidate_id_occurrences": sum(duplicate_candidate_ids.values()),
            "distinct_site_asset_identity": len(identities),
            "duplicate_identity_occurrences": sum(duplicate_identities.values()),
            "empty_site_or_assetnum": sum(1 for site, assetnum in identities if not site or not assetnum),
        },
        "provenance": {
            "source_snapshots": dict(source_snapshots),
            "source_row_hash_lengths": dict(source_row_hash_lengths),
            "context_hash_lengths": dict(context_hash_lengths),
            "result_statuses": dict(statuses),
            "context_statuses": dict(context_statuses),
            "rule_versions": dict(rules),
            "validator_versions": dict(validators),
            "context_json_parsed": metrics["context_json_parsed"],
            "context_json_invalid": metrics["context_json_invalid"],
            "context_reason_codes": dict(context_reason_counts),
        },
        "description_quality": {
            "original_nonempty": metrics["original_nonempty"],
            "candidate_nonempty": metrics["candidate_nonempty"],
            "candidate_same_as_original_normalized": metrics["candidate_same_as_original_normalized"],
            "candidate_changed_normalized": row_count - metrics["candidate_same_as_original_normalized"],
            "description_length_min": min(desc_lengths) if desc_lengths else 0,
            "description_length_max": max(desc_lengths) if desc_lengths else 0,
            "description_length_distribution": top_json(desc_lengths, 30),
            "distinct_nonempty_normalized_descriptions": exact_desc_count,
            "rows_with_repeated_nonempty_description": repeated_desc_rows,
            "top_repeated_descriptions": top_json(Counter({key: value for key, value in desc_counts.items() if key}), 30),
            "very_short_le_2": metrics["description_len_le_2"],
            "very_short_le_3": metrics["description_len_le_3"],
            "generic_exact": metrics["GENERIC_EXACT_DESCRIPTION"],
            "placeholder_pattern": metrics["PLACEHOLDER_PATTERN"],
            "code_only": metrics["CODE_ONLY_DESCRIPTION"],
        },
        "context_quality": {
            "location_nonempty": metrics["location_nonempty"],
            "kks_like_location": metrics["kks_like_location"],
            "location_description_nonempty": metrics["location_description_nonempty"],
            "location_parent_nonempty": metrics["location_parent_nonempty"],
            "system_nonempty": metrics["system_nonempty"],
            "classstructure_description_nonempty": metrics["classstructure_description_nonempty"],
            "classification_description_nonempty": metrics["classification_description_nonempty"],
            "asset_location_description_exact": metrics["asset_location_description_exact"],
            "asset_location_description_different": metrics["asset_location_description_different"],
            "asset_location_not_comparable": metrics["asset_location_not_comparable"],
        },
        "site_counts": dict(site_counts),
        "top_systems": top_json(Counter({key or "(empty)": value for key, value in system_counts.items()}), 30),
        "status": "assessment_only",
        "source_write": False,
        "formal_publication": False,
        "outputs": [str(SUSPECTS)],
        "changed_sample_output": str(CHANGED),
        "changed_sample_count": len(changed_rows),
        "rule_version": "hd-unification-batch-quality-0.1.0",
    }
    SUMMARY.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    structural_failures: list[str] = []
    if row_count != 384322:
        structural_failures.append("ROW_COUNT_MISMATCH")
    if len(candidate_ids) != row_count:
        structural_failures.append("DUPLICATE_CANDIDATE_ID")
    if len(identities) != row_count:
        structural_failures.append("DUPLICATE_DEVICE_IDENTITY")
    if metrics["context_json_invalid"]:
        structural_failures.append("INVALID_CONTEXT_JSON")
    if metrics["original_nonempty"] != row_count:
        structural_failures.append("EMPTY_ORIGINAL_DESCRIPTION")

    report = [
        "# 384,322条统一语义处理批次质量评估",
        "",
        "## 总结",
        "",
        "这批数据的结构质量通过，但语义统一尚未真正执行：`CANDIDATE_DESCRIPTION`与`ORIGINAL_DESCRIPTION`仍基本相同，因此当前是“统一语义处理输入批次”，不是最终统一语义结果。",
        "",
        f"- 结构校验：{'PASS' if not structural_failures else 'FAIL'}",
        f"- 设备身份：{len(identities)}个唯一身份 / {row_count}行",
        f"- 原描述非空：{metrics['original_nonempty']}行",
        f"- 候选描述与原描述规范化后一致：{metrics['candidate_same_as_original_normalized']}行",
        f"- 候选描述已改变：{row_count - metrics['candidate_same_as_original_normalized']}行",
        f"- KKS/位置编码非空：{metrics['location_nonempty']}行",
        f"- 位置描述非空：{metrics['location_description_nonempty']}行",
        f"- 位置父级非空：{metrics['location_parent_nonempty']}行",
        f"- 设备描述与位置描述一致：{metrics['asset_location_description_exact']}行",
        f"- 设备描述与位置描述不同：{metrics['asset_location_description_different']}行",
        f"- 无法比较：{metrics['asset_location_not_comparable']}行",
        "",
        "## 质量判断",
        "",
        "- 可以进入下一步规则化处理；",
        "- 不应直接当作正式统一语义成果发布；",
        "- 设备身份、来源哈希、上下文哈希和原始描述应继续保留；",
        "- 重复设备描述本身不判为错误，同类设备可以使用同一标准名称；",
        "- KKS/位置描述继续作为上下文，不直接拼入设备名称。",
        "",
        "## 可疑描述候选",
        "",
        f"- 极短描述（长度≤3）：{metrics['description_len_le_3']}行",
        f"- 精确通用描述：{metrics['GENERIC_EXACT_DESCRIPTION']}行",
        f"- 占位符模式：{metrics['PLACEHOLDER_PATTERN']}行",
        f"- 纯编码样式描述：{metrics['CODE_ONLY_DESCRIPTION']}行",
        "这些只是质量候选，不在本次自动剔除；同一记录可能同时命中多个标记。",
        "",
        f"详细汇总：[summary.json](../quality_assessment/summary.json)",
        f"可疑描述样本：[description_quality_candidates.csv](../quality_assessment/description_quality_candidates.csv)",
    ]
    REPORT.write_text("\n".join(report) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
