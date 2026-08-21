"""Generate source-grounded semantic candidates for the filtered HD batch.

Only deterministic, evidence-preserving operations are allowed here. The
source description is retained, location/KKS/classification context is used
for validation, and no context is concatenated into the equipment name.
"""
from __future__ import annotations

import csv
import hashlib
import json
import pathlib
import re
import unicodedata
from collections import Counter
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parent
INPUT = ROOT / "quality_scope_after_suspect_exclusion" / "hd_quality_candidates_for_unification.csv"
TERMINOLOGY = ROOT / "terminology" / "terminology_candidates.csv"
OUTPUT_DIR = ROOT / "semantic_candidates"
OUTPUT = OUTPUT_DIR / "hd_semantic_candidates.csv"
MANIFEST = OUTPUT_DIR / "manifest.json"
VERIFICATION = OUTPUT_DIR / "verification.json"
REPORT = ROOT / "reports" / "semantic_candidate_generation_20260812.md"
RULE_VERSION = "hd-semantic-rules-0.2.0"
VALIDATOR_VERSION = "hd-semantic-validator-0.2.0"

CODE_ONLY_RE = re.compile(r"^[A-Za-z0-9_.:/-]+$")
GENERIC_TERMS = {"备用", "设备", "部件", "阀", "泵", "电机", "开关", "柜", "模块", "测试", "未知", "暂无", "待定"}


def clean(value: object) -> str:
    return "" if value is None else str(value).strip()


def normalize(value: object) -> str:
    text = unicodedata.normalize("NFKC", clean(value))
    text = re.sub(r"[\t\r\n]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text.strip(" ,，;；:：、·.。-—")


def is_code_only(value: str) -> bool:
    return bool(value and CODE_ONLY_RE.fullmatch(value) and not any("\u4e00" <= char <= "\u9fff" for char in value))


def load_confirmed_terms() -> list[tuple[str, str, str]]:
    if not TERMINOLOGY.exists():
        return []
    rules: list[tuple[str, str, str]] = []
    with TERMINOLOGY.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            status = clean(row.get("STATUS")).lower()
            source = clean(row.get("SOURCE_TERM"))
            target = clean(row.get("CANDIDATE_STANDARD_TERM"))
            term_id = clean(row.get("TERM_ID"))
            if status in {"confirmed", "active"} and source and target and source != target:
                rules.append((source, target, term_id))
    return rules


def parse_context(row: dict[str, str]) -> dict[str, object]:
    try:
        context = json.loads(row.get("CONTEXT_JSON") or "{}")
    except (json.JSONDecodeError, TypeError):
        return {"valid": False, "location": {}, "hierarchy": {}, "classification": {}}
    return {
        "valid": isinstance(context, dict),
        "location": context.get("location") or {},
        "hierarchy": context.get("location_hierarchy") or {},
        "classification": context.get("classification") or {},
        "class_structure": context.get("class_structure") or {},
    }


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    run_id = "hd-semantic-candidate-generation-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    term_rules = load_confirmed_terms()
    term_rule_ids = [item[2] for item in term_rules]
    counts: Counter[str] = Counter()
    reason_counts: Counter[str] = Counter()
    candidate_ids: set[str] = set()
    identities: set[tuple[str, str]] = set()
    input_rows = 0
    changed_from_original = 0
    changed_from_input_candidate = 0
    context_invalid = 0

    with INPUT.open(encoding="utf-8-sig", newline="") as source_handle:
        reader = csv.DictReader(source_handle)
        if not reader.fieldnames:
            raise ValueError("输入批次缺少表头")
        input_fields = list(reader.fieldnames)
        added_fields = [
            "UNIFIED_DESCRIPTION",
            "SEMANTIC_ACTION",
            "SEMANTIC_CONFIDENCE",
            "SEMANTIC_RESULT_STATUS",
            "SEMANTIC_REASON_CODES",
            "CONTEXT_EVIDENCE_LEVEL",
            "APPLIED_TERM_RULE_IDS",
            "SEMANTIC_RULE_VERSION",
            "SEMANTIC_VALIDATOR_VERSION",
            "SEMANTIC_RUN_ID",
            "SEMANTIC_CANDIDATE_HASH",
            "SEMANTIC_EVIDENCE_JSON",
        ]
        with OUTPUT.open("w", encoding="utf-8-sig", newline="") as output_handle:
            writer = csv.DictWriter(output_handle, fieldnames=input_fields + added_fields)
            writer.writeheader()
            for row in reader:
                input_rows += 1
                candidate_id = clean(row.get("CANDIDATE_ID"))
                site = clean(row.get("SITEID"))
                assetnum = clean(row.get("ASSETNUM"))
                original = clean(row.get("ORIGINAL_DESCRIPTION"))
                input_candidate = clean(row.get("CANDIDATE_DESCRIPTION"))
                candidate_ids.add(candidate_id)
                identities.add((site, assetnum))

                context = parse_context(row)
                if not context["valid"]:
                    context_invalid += 1
                location = context["location"]
                hierarchy = context["hierarchy"]
                classification = context["classification"]
                class_structure = context["class_structure"]
                location_code = clean(location.get("LOCATION"))
                location_parent = clean(hierarchy.get("PARENT"))
                location_description = clean(row.get("LOCATION_DESCRIPTION")) or clean(location.get("DESCRIPTION"))
                classification_description = clean(row.get("CLASSIFICATION_DESCRIPTION")) or clean(classification.get("DESCRIPTION"))
                class_structure_description = clean(row.get("CLASSSTRUCTURE_DESCRIPTION")) or clean(class_structure.get("DESCRIPTION"))

                unified = normalize(input_candidate or original)
                applied_rules: list[str] = []
                for source_term, target_term, term_id in term_rules:
                    if source_term in unified:
                        unified = unified.replace(source_term, target_term)
                        applied_rules.append(term_id)

                reasons: list[str] = []
                if unified != normalize(input_candidate):
                    reasons.append("SAFE_FORMAT_NORMALIZATION")
                    changed_from_input_candidate += 1
                if unified != normalize(original):
                    changed_from_original += 1
                if applied_rules:
                    reasons.append("CONFIRMED_TERMINOLOGY_RULE")
                else:
                    reasons.append("NO_CONFIRMED_SEMANTIC_REWRITE")

                hard_failures: list[str] = []
                if not site or not assetnum:
                    hard_failures.append("MISSING_STABLE_IDENTITY")
                if not candidate_id:
                    hard_failures.append("MISSING_CANDIDATE_ID")
                if not unified:
                    hard_failures.append("EMPTY_UNIFIED_DESCRIPTION")
                if len(unified) > 500:
                    hard_failures.append("DESCRIPTION_TOO_LONG")
                if len(unified) < 2:
                    hard_failures.append("INSUFFICIENT_DESCRIPTION")
                if is_code_only(unified):
                    hard_failures.append("CODE_ONLY_DESCRIPTION")
                if unified in GENERIC_TERMS:
                    hard_failures.append("GENERIC_DESCRIPTION")
                if not context["valid"]:
                    hard_failures.append("INVALID_CONTEXT_JSON")

                if hard_failures:
                    status = "blocked"
                    confidence = "low"
                    reasons.extend(hard_failures)
                else:
                    status = "candidate"
                    confidence = "high"

                evidence = {
                    "original_description": original,
                    "input_candidate_description": input_candidate,
                    "location_code": location_code,
                    "location_description": location_description,
                    "location_parent": location_parent,
                    "classification_description": classification_description,
                    "class_structure_description": class_structure_description,
                    "spec_count": clean(row.get("SPEC_COUNT")),
                    "feature_count": clean(row.get("FEATURE_COUNT")),
                    "parent_asset_count": clean(row.get("PARENT_ASSET_COUNT")),
                    "relation_count": clean(row.get("RELATION_COUNT")),
                }
                evidence_level = "strong" if location_code and (location_parent or location_description) else "basic"
                if classification_description or class_structure_description:
                    evidence_level = "strong"
                result = dict(row)
                result.update(
                    {
                        "UNIFIED_DESCRIPTION": unified,
                        "SEMANTIC_ACTION": "APPLY_CONFIRMED_TERM_RULE" if applied_rules else ("SAFE_FORMAT_NORMALIZATION" if unified != normalize(input_candidate) else "PRESERVE_SOURCE_DESCRIPTION"),
                        "SEMANTIC_CONFIDENCE": confidence,
                        "SEMANTIC_RESULT_STATUS": status,
                        "SEMANTIC_REASON_CODES": "|".join(dict.fromkeys(reasons)),
                        "CONTEXT_EVIDENCE_LEVEL": evidence_level,
                        "APPLIED_TERM_RULE_IDS": "|".join(applied_rules),
                        "SEMANTIC_RULE_VERSION": RULE_VERSION,
                        "SEMANTIC_VALIDATOR_VERSION": VALIDATOR_VERSION,
                        "SEMANTIC_RUN_ID": run_id,
                        "SEMANTIC_CANDIDATE_HASH": hashlib.sha256((candidate_id + "|" + unified).encode("utf-8")).hexdigest(),
                        "SEMANTIC_EVIDENCE_JSON": json.dumps(evidence, ensure_ascii=False, separators=(",", ":")),
                    }
                )
                writer.writerow(result)
                counts[status] += 1
                for reason in dict.fromkeys(reasons):
                    reason_counts[reason] += 1

    verification = {
        "status": "PASS"
        if input_rows > 0
        and len(candidate_ids) == input_rows
        and len(identities) == input_rows
        and context_invalid == 0
        and counts["blocked"] == 0
        else "FAIL",
        "input_rows": input_rows,
        "output_rows": sum(counts.values()),
        "distinct_candidate_ids": len(candidate_ids),
        "distinct_site_asset_identities": len(identities),
        "context_invalid": context_invalid,
        "result_status_counts": dict(counts),
        "changed_from_original": changed_from_original,
        "changed_from_input_candidate": changed_from_input_candidate,
        "confirmed_term_rule_count": len(term_rules),
        "source_write": False,
        "formal_publication": False,
    }
    VERIFICATION.write_text(json.dumps(verification, ensure_ascii=False, indent=2), encoding="utf-8")

    manifest = {
        "run_id": run_id,
        "input_file": str(INPUT),
        "input_sha256": hashlib.sha256(INPUT.read_bytes()).hexdigest(),
        "input_rows": input_rows,
        "output_file": str(OUTPUT),
        "output_sha256": hashlib.sha256(OUTPUT.read_bytes()).hexdigest(),
        "output_rows": sum(counts.values()),
        "rule_version": RULE_VERSION,
        "validator_version": VALIDATOR_VERSION,
        "terminology_file": str(TERMINOLOGY),
        "confirmed_term_rule_count": len(term_rules),
        "reason_counts": dict(reason_counts),
        "source_write": False,
        "formal_publication": False,
        "status": "candidate_only",
        "approval_required": True,
    }
    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    report = [
        f"# {input_rows:,} 条统一语义候选生成结果",
        "",
        "本次只执行来源可追溯的确定性规则：保留原描述，不把位置、KKS、分类或父级擅自拼入设备名称；未发现已确认的本地术语替换规则，因此没有批量改写专业术语。",
        "",
        "## 结果",
        "",
        f"- 输入/输出：{input_rows} / {sum(counts.values())} 条",
        f"- candidate：{counts['candidate']} 条",
        f"- needs_review：{counts['needs_review']} 条",
        f"- blocked：{counts['blocked']} 条",
        f"- 相对原描述发生变化：{changed_from_original} 条",
        f"- 相对上一批候选发生安全格式变化：{changed_from_input_candidate} 条",
        f"- 已确认术语规则：{len(term_rules)} 条",
        f"- 校验：{verification['status']}",
        "",
        "## 语义边界",
        "",
        "当前输出是统一语义候选层，不是正式发布层。设备身份、原描述、上下文、规则版本、验证器版本和证据均保留；正式发布前仍需审核批准。",
        "",
        f"候选文件：[{OUTPUT.name}](../semantic_candidates/{OUTPUT.name})",
        f"清单：[{MANIFEST.name}](../semantic_candidates/{MANIFEST.name})",
        f"校验：[{VERIFICATION.name}](../semantic_candidates/{VERIFICATION.name})",
    ]
    REPORT.write_text("\n".join(report) + "\n", encoding="utf-8")
    print(json.dumps({"manifest": manifest, "verification": verification}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
