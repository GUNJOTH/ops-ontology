"""Derive a new unification scope by excluding flagged descriptions.

The source batch and frozen quality batch are preserved. This script only
creates a downstream derived scope and an auditable exclusion list.
"""
from __future__ import annotations

import csv
import hashlib
import json
import pathlib
from collections import Counter
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parent
INPUT = ROOT / "quality_scope" / "hd_quality_candidates_for_unification.csv"
EXCLUSION_INPUT = ROOT / "quality_assessment" / "description_quality_candidates.csv"
OUTPUT_DIR = ROOT / "quality_scope_after_suspect_exclusion"
OUTPUT = OUTPUT_DIR / "hd_quality_candidates_for_unification.csv"
EXCLUDED = OUTPUT_DIR / "excluded_suspect_descriptions_20260812.csv"
MANIFEST = OUTPUT_DIR / "manifest.json"
VERIFICATION = OUTPUT_DIR / "verification.json"
REPORT = ROOT / "reports" / "quality_scope_after_suspect_exclusion_20260812.md"


def sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def clean(value: object) -> str:
    return "" if value is None else str(value).strip()


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    exclusion_rows: dict[str, dict[str, str]] = {}
    flag_counts: Counter[str] = Counter()
    with EXCLUSION_INPUT.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            candidate_id = clean(row.get("CANDIDATE_ID"))
            if not candidate_id:
                raise ValueError("exclusion row has empty CANDIDATE_ID")
            if candidate_id in exclusion_rows:
                raise ValueError(f"duplicate exclusion CANDIDATE_ID: {candidate_id}")
            exclusion_rows[candidate_id] = row
            for flag in clean(row.get("FLAGS")).split(";"):
                if flag:
                    flag_counts[flag] += 1

    input_rows = 0
    output_rows = 0
    excluded_rows = 0
    unmatched_exclusion_ids = set(exclusion_rows)
    output_candidate_ids: set[str] = set()
    output_identities: set[tuple[str, str]] = set()
    excluded_details: list[dict[str, str]] = []

    with INPUT.open(encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        if not reader.fieldnames:
            raise ValueError("input batch has no header")
        fieldnames = list(reader.fieldnames)
        with OUTPUT.open("w", encoding="utf-8-sig", newline="") as target:
            writer = csv.DictWriter(target, fieldnames=fieldnames)
            writer.writeheader()
            for row in reader:
                input_rows += 1
                candidate_id = clean(row.get("CANDIDATE_ID"))
                if candidate_id in exclusion_rows:
                    excluded_rows += 1
                    unmatched_exclusion_ids.discard(candidate_id)
                    exclusion = exclusion_rows[candidate_id]
                    excluded_details.append(
                        {
                            "CANDIDATE_ID": candidate_id,
                            "SITEID": clean(row.get("SITEID")),
                            "ASSETNUM": clean(row.get("ASSETNUM")),
                            "ORIGINAL_DESCRIPTION": clean(row.get("ORIGINAL_DESCRIPTION")),
                            "LOCATION_CODE": clean(exclusion.get("LOCATION_CODE")),
                            "LOCATION_DESCRIPTION": clean(exclusion.get("LOCATION_DESCRIPTION")),
                            "FLAGS": clean(exclusion.get("FLAGS")),
                            "EXCLUSION_RULE": "description_quality_flagged",
                        }
                    )
                    continue
                writer.writerow(row)
                output_rows += 1
                output_candidate_ids.add(candidate_id)
                output_identities.add((clean(row.get("SITEID")), clean(row.get("ASSETNUM"))))

    excluded_fields = [
        "CANDIDATE_ID",
        "SITEID",
        "ASSETNUM",
        "ORIGINAL_DESCRIPTION",
        "LOCATION_CODE",
        "LOCATION_DESCRIPTION",
        "FLAGS",
        "EXCLUSION_RULE",
    ]
    with EXCLUDED.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=excluded_fields)
        writer.writeheader()
        writer.writerows(excluded_details)

    verification = {
        "status": "PASS"
        if (
            input_rows == 384322
            and excluded_rows == len(exclusion_rows)
            and not unmatched_exclusion_ids
            and output_rows == input_rows - excluded_rows
            and len(output_candidate_ids) == output_rows
            and len(output_identities) == output_rows
        )
        else "FAIL",
        "input_rows": input_rows,
        "requested_exclusion_rows": len(exclusion_rows),
        "matched_exclusion_rows": excluded_rows,
        "unmatched_exclusion_ids": len(unmatched_exclusion_ids),
        "kept_rows": output_rows,
        "reconciled": output_rows + excluded_rows == input_rows,
        "kept_distinct_candidate_ids": len(output_candidate_ids),
        "kept_distinct_identities": len(output_identities),
        "source_write": False,
        "formal_publication": False,
    }
    VERIFICATION.write_text(json.dumps(verification, ensure_ascii=False, indent=2), encoding="utf-8")

    manifest = {
        "run_id": "hd-quality-scope-exclude-suspect-descriptions-"
        + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
        "input_file": str(INPUT),
        "input_file_sha256": sha256(INPUT),
        "input_rows": input_rows,
        "exclusion_input_file": str(EXCLUSION_INPUT),
        "exclusion_input_file_sha256": sha256(EXCLUSION_INPUT),
        "exclusion_rule": "exclude any row flagged by description quality assessment",
        "flag_counts": dict(flag_counts),
        "excluded_rows": excluded_rows,
        "excluded_file": str(EXCLUDED),
        "excluded_file_sha256": sha256(EXCLUDED),
        "output_file": str(OUTPUT),
        "output_file_sha256": sha256(OUTPUT),
        "output_rows": output_rows,
        "source_write": False,
        "formal_publication": False,
        "input_scope_preserved": True,
        "status": "derived_unification_scope_after_description_quality_exclusion",
        "rule_version": "hd-quality-scope-0.3.0",
    }
    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    report = [
        "# 描述质量候选剔除后的统一语义处理范围",
        "",
        f"本次从 384,322 条统一语义处理输入中剔除 {excluded_rows} 条命中描述质量标记的记录，保留 {output_rows} 条。",
        "",
        "## 剔除规则",
        "",
        "凡命中极短描述、通用描述、占位符模式或纯编码描述任一标记的记录，暂不进入统一语义规则处理。",
        "",
        "## 标记统计",
        "",
    ]
    for flag, count in flag_counts.most_common():
        report.append(f"- {flag}: {count} 条（标记之间可能重叠）")
    report.extend(
        [
            "",
            "## 校验结果",
            "",
            f"- 输入：{input_rows} 条",
            f"- 剔除：{excluded_rows} 条",
            f"- 保留：{output_rows} 条",
            f"- 未匹配剔除 ID：{len(unmatched_exclusion_ids)} 条",
            f"- 结果：{verification['status']}",
            "- 源表未写入，原质量批次和上一层处理范围均保留。",
            "- 本结果仍是派生处理范围，不是正式发布结果。",
            "",
            f"汇总：[{MANIFEST.name}](../quality_scope_after_suspect_exclusion/{MANIFEST.name})",
            f"校验：[{VERIFICATION.name}](../quality_scope_after_suspect_exclusion/{VERIFICATION.name})",
            f"剔除清单：[{EXCLUDED.name}](../quality_scope_after_suspect_exclusion/{EXCLUDED.name})",
        ]
    )
    REPORT.write_text("\n".join(report) + "\n", encoding="utf-8")
    print(json.dumps({"manifest": manifest, "verification": verification}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
