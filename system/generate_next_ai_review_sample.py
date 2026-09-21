"""Build and batch-judge a representative AI review sample from remaining diffs.

This is recommendation-only. It never updates candidate, review, or
publication tables.
"""
from __future__ import annotations

import csv
import hashlib
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path

from pipeline.contracts import connect_readonly

ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parent
DB = ROOT / "data" / "semantic_workflow.sqlite3"
OUTPUT_DIR = PROJECT_ROOT / "pilots" / "HD_SAAS" / "next_ai_review"
SAMPLE_CSV = OUTPUT_DIR / "sample_200_judgment.csv"
SUMMARY_JSON = OUTPUT_DIR / "summary.json"
MANIFEST_JSON = OUTPUT_DIR / "manifest.json"
SAMPLE_TARGET = 200
JUDGE_VERSION = "semantic-batch-judge-20260812-v2"

ROMAN = set("\u2160\u2161\u2162\u2163\u2164\u2165\u2166\u2167\u2168\u2169")
SAFE_PUNCT = {"\ufe51": "\u3001", "\uff1b": ";", "\uff1c": "<", "\uff1e": ">"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def diff_signature(original: str, candidate: str) -> str:
    parts: list[str] = []
    for tag, i1, i2, j1, j2 in SequenceMatcher(None, original, candidate).get_opcodes():
        if tag != "equal":
            parts.append(f"{tag}:{original[i1:i2]}->{candidate[j1:j2]}")
    return " | ".join(parts)


def category(original: str, candidate: str) -> str:
    if any(char in original for char in ROMAN):
        return "roman_numeral_or_section_marker"
    if "℃" in original or "°C" in candidate:
        return "temperature_unit"
    if "？" in original or "?" in candidate:
        return "question_or_unknown_marker"
    if "-" in original and "-" not in candidate:
        return "minus_or_signed_value_deleted"
    if any(char in original for char in "①②③④⑤⑥⑦⑧⑨⑩"):
        return "circled_number_marker"
    if any(char in original for char in "＋～"):
        return "plus_or_range_marker"
    if original and candidate and all(SAFE_PUNCT.get(char, char) == out for char, out in zip(original, candidate)) and len(original) == len(candidate):
        return "safe_punctuation_shape"
    if original != candidate:
        return "mixed_or_other_rewrite"
    return "unchanged"


def judge(original: str, candidate: str, kind: str) -> tuple[str, str, str, str]:
    flags: list[str] = []
    if kind == "safe_punctuation_shape":
        return "接受候选", "0.94", "仅发生已定义标点形态转换，字符数量和设备词序未变。", "cluster_review_then_replay"
    if kind == "minus_or_signed_value_deleted":
        flags.append("signed_value_or_negative_level")
        return "保留原文", "0.99", "候选删除负号，可能改变负标高、负电压或方向含义。", "preserve_source"
    if kind == "roman_numeral_or_section_marker":
        flags.append("section_phase_or_bus_marker")
        return "需要复核", "0.85", "罗马数字可能表示母线段、机组段、相别或设备序号，不能直接归一化。", "context_review"
    if kind == "temperature_unit":
        flags.append("unit_semantics")
        return "需要复核", "0.86", "℃到°C涉及单位写法，需确认系统统一单位规则和原始工程约定。", "unit_rule_review"
    if kind == "question_or_unknown_marker":
        flags.append("unknown_or_placeholder")
        return "需要复核", "0.90", "问号可能是未知值、占位符或录入标记，不能仅做字符替换。", "context_review"
    if kind == "circled_number_marker":
        flags.append("number_marker")
        return "需要复核", "0.82", "带圈数字可能是门禁、位置或设备序号，需结合位置和分类确认。", "context_review"
    if kind == "plus_or_range_marker":
        flags.append("range_or_polarity_marker")
        return "需要复核", "0.80", "加号或波浪号可能表示极性、范围或标高关系，不能直接改写。", "context_review"
    return "需要复核", "0.60", "存在非确认的混合字符或词语变化。", "context_review"


def select_sample(rows: list[dict[str, str]], target: int) -> list[dict[str, str]]:
    strata: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        strata[f"{row['SITEID']} / {row['DIFF_CATEGORY']}"] .append(row)
    for bucket in strata.values():
        bucket.sort(key=lambda row: (row["ASSETNUM"], row["CANDIDATE_ID"]))
    selected: list[dict[str, str]] = []
    keys = sorted(strata)
    for key in keys:
        if strata[key] and len(selected) < target:
            selected.append(strata[key].pop(0))
    while len(selected) < target:
        progressed = False
        for key in keys:
            if strata[key]:
                selected.append(strata[key].pop(0))
                progressed = True
                if len(selected) == target:
                    break
        if not progressed:
            break
    return selected


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    connection = connect_readonly(DB)
    rows = connection.execute(
        """
        SELECT c.candidate_id,c.batch_id,c.original_description,c.candidate_description,
          c.semantic_action,c.reason_codes_json,c.confidence,c.validator_status,
          c.review_state,c.publication_state,c.rule_version,c.validator_version,
          d.source_snapshot_id,d.source_schema,d.source_asset_id,d.site_id,d.asset_number,
          d.source_row_hash,d.context_hash,d.location_code,d.location_description,d.location_parent,
          d.class_structure_description,d.classification_description
        FROM semantic_candidate c JOIN device_identity d ON d.device_id=c.device_id
        WHERE c.publication_state='unpublished' AND c.review_state='pending'
          AND c.validator_status='candidate' AND c.confidence='high'
          AND trim(d.original_description)<>''
          AND c.original_description<>c.candidate_description
        ORDER BY d.site_id,d.asset_number,c.candidate_id
        """
    ).fetchall()
    connection.close()

    all_rows: list[dict[str, str]] = []
    for row in rows:
        original = row["original_description"] or ""
        candidate = row["candidate_description"] or ""
        kind = category(original, candidate)
        decision, confidence, reason, gate = judge(original, candidate, kind)
        all_rows.append({
            "SITEID": row["site_id"] or "", "ASSETNUM": row["asset_number"] or "", "ASSETID": row["source_asset_id"] or "",
            "CANDIDATE_ID": row["candidate_id"], "BATCH_ID": row["batch_id"], "SOURCE_SNAPSHOT_ID": row["source_snapshot_id"],
            "SOURCE_SCHEMA": row["source_schema"] or "", "SOURCE_ROW_HASH": row["source_row_hash"] or "", "CONTEXT_HASH": row["context_hash"] or "",
            "ORIGINAL_DESCRIPTION": original, "EXISTING_CANDIDATE_DESCRIPTION": candidate,
            "DIFF_CATEGORY": kind, "DIFF_SIGNATURE": diff_signature(original, candidate),
            "LOCATION_CODE": row["location_code"] or "", "LOCATION_DESCRIPTION": row["location_description"] or "", "LOCATION_PARENT": row["location_parent"] or "",
            "CLASSSTRUCTURE_DESCRIPTION": row["class_structure_description"] or "", "CLASSIFICATION_DESCRIPTION": row["classification_description"] or "",
            "CONFIDENCE": row["confidence"], "VALIDATOR_STATUS": row["validator_status"], "REVIEW_STATE": row["review_state"], "PUBLICATION_STATE": row["publication_state"],
            "AI_DECISION": decision, "AI_CONFIDENCE": confidence, "AI_REASON": reason, "AI_GATE": gate,
            "JUDGE_VERSION": JUDGE_VERSION, "JUDGMENT_STATUS": "recommendation_only",
        })
    sample_rows = select_sample(all_rows, min(SAMPLE_TARGET, len(all_rows)))
    columns = list(sample_rows[0].keys()) if sample_rows else []
    with SAMPLE_CSV.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(sample_rows)

    summary = {
        "remaining_changed_rows": len(all_rows),
        "sample_rows": len(sample_rows),
        "all_by_category": dict(Counter(row["DIFF_CATEGORY"] for row in all_rows)),
        "sample_by_category": dict(Counter(row["DIFF_CATEGORY"] for row in sample_rows)),
        "sample_by_decision": dict(Counter(row["AI_DECISION"] for row in sample_rows)),
        "sample_by_site": dict(Counter(row["SITEID"] for row in sample_rows)),
        "source_write": False, "formal_publication": False,
        "judgment_status": "recommendation_only",
        "next_gate": "cluster-level confirmation and replay required before publication",
    }
    manifest = {
        "sample_id": f"next-ai-review-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}",
        "generated_at_utc": utc_now(), "source_database": str(DB),
        "source_scope": "unpublished + pending + candidate + high confidence + original != candidate",
        "sample_rows": len(sample_rows), "sample_file": str(SAMPLE_CSV), "sample_sha256": sha256(SAMPLE_CSV),
        "judge_version": JUDGE_VERSION, "decision_labels": ["保留原文", "接受候选", "需要复核"],
        "source_write": False, "formal_publication": False, "judgment_status": "recommendation_only", "summary": summary,
    }
    SUMMARY_JSON.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    MANIFEST_JSON.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"manifest": str(MANIFEST_JSON), **summary}, ensure_ascii=False))


if __name__ == "__main__":
    main()
