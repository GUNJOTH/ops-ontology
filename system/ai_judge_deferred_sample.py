"""AI-assisted batch judgment for the 200-row deferred semantic sample.

This is a recommendation layer only. It reads the sample and writes an
auditable decision package; it does not update candidate, review, or
publication tables.
"""
from __future__ import annotations

import csv
import hashlib
import json
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path


ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parent
DB = ROOT / "data" / "semantic_workflow.sqlite3"
INPUT_DIR = PROJECT_ROOT / "pilots" / "HD_SAAS" / "deferred_semantic_review"
INPUT_CSV = INPUT_DIR / "sample_200.csv"
OUTPUT_DIR = PROJECT_ROOT / "pilots" / "HD_SAAS" / "ai_judgment"
OUTPUT_CSV = OUTPUT_DIR / "sample_200_judgment.csv"
SUMMARY_JSON = OUTPUT_DIR / "summary.json"
MANIFEST_JSON = OUTPUT_DIR / "manifest.json"
JUDGE_VERSION = "semantic-batch-judge-20260812-v1"


# These mappings preserve the semantic token while normalizing a presentation
# form. Roman numerals and question marks are intentionally excluded because
# they can encode section/phase identity or an unresolved source value.
SAFE_MAPPINGS = {
    "＃": "#", "﹑": "、", "＋": "+", "～": "~", "＜": "<", "＞": ">", "；": ";",
}
SAFE_MAPPINGS.update({chr(ord("０") + i): str(i) for i in range(10)})
SAFE_MAPPINGS.update({ch: chr(ord("A") + i) for i, ch in enumerate("ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺ")})


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


def only_safe_mapping(original: str, candidate: str) -> bool:
    mapped = "".join(SAFE_MAPPINGS.get(char, char) for char in original)
    return mapped == candidate and original != candidate


def only_terminal_punctuation_deletion(original: str, candidate: str) -> bool:
    if not original.endswith((":", "·")):
        return False
    return original[:-1] == candidate


def deleted_sign(original: str, candidate: str) -> bool:
    # A removed hyphen can change negative elevation, polarity, or a signed
    # voltage. Treat it as source-preserving even when it is terminal.
    return "-" in original and "-" not in candidate and original.replace("-", "", 1) == candidate


def semantic_marker_flags(original: str, candidate: str) -> list[str]:
    flags: list[str] = []
    if any(char in original for char in "ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ"):
        flags.append("roman_numeral_section_or_phase")
    if "？" in original or "?" in candidate:
        flags.append("question_mark_or_unknown_token")
    if "℃" in original:
        flags.append("temperature_unit")
    if any(char in original for char in "①②③④⑤⑥⑦⑧⑨⑩"):
        flags.append("circled_number_marker")
    if original.startswith("-") or re.search(r"\b-\d", original):
        flags.append("signed_value_or_negative_level")
    return flags


def judge(row: dict[str, str]) -> dict[str, str]:
    original = row["ORIGINAL_DESCRIPTION"]
    candidate = row["EXISTING_CANDIDATE_DESCRIPTION"]
    flags = semantic_marker_flags(original, candidate)
    signature = diff_signature(original, candidate)

    if deleted_sign(original, candidate):
        decision = "保留原文"
        confidence = "0.99"
        reason = "候选删除了负号，可能改变负标高、负电压或方向含义。"
        gate = "block_auto_publish"
    elif only_safe_mapping(original, candidate):
        decision = "接受候选"
        confidence = "0.98"
        reason = "仅发生已定义的全角/半角或等价标点转换，未改变字符数量和语义词序。"
        gate = "eligible_for_cluster_approval"
    elif only_terminal_punctuation_deletion(original, candidate):
        decision = "接受候选"
        confidence = "0.93"
        reason = "仅删除描述末尾的孤立冒号/中点，不影响设备名称主体；建议作为独立规则簇审批。"
        gate = "eligible_for_cluster_approval_with_rule_confirmation"
    elif "roman_numeral_section_or_phase" in flags:
        decision = "需要复核"
        confidence = "0.72"
        reason = "罗马数字可能表示母线段、机组段、相别或设备序号，不能仅凭字符归一化确认。"
        gate = "human_or_context_review"
    elif "question_mark_or_unknown_token" in flags:
        decision = "需要复核"
        confidence = "0.68"
        reason = "问号可能是未知值、占位符或原始录入标记，候选只改变编码形式，未消除不确定性。"
        gate = "human_or_context_review"
    else:
        decision = "需要复核"
        confidence = "0.60"
        reason = "候选存在非等价删除或词语变化，缺少已确认的确定性规则。"
        gate = "human_or_context_review"

    output = dict(row)
    output.update({
        "AI_DECISION": decision,
        "AI_CONFIDENCE": confidence,
        "AI_REASON": reason,
        "AI_GATE": gate,
        "DIFF_SIGNATURE": signature,
        "RISK_FLAGS": "|".join(flags),
        "JUDGE_VERSION": JUDGE_VERSION,
        "JUDGMENT_STATUS": "recommendation_only",
    })
    return output


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    if not INPUT_CSV.exists():
        raise SystemExit(f"Missing sample: {INPUT_CSV}")
    with INPUT_CSV.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if len(rows) != 200:
        raise SystemExit(f"Expected 200 sample rows, found {len(rows)}")
    if len({row["CANDIDATE_ID"] for row in rows}) != 200:
        raise SystemExit("Sample contains duplicate candidate IDs")

    # Reconcile every row with the current local database before judging it.
    connection = sqlite3.connect(str(DB))
    connection.row_factory = sqlite3.Row
    candidate_ids = [row["CANDIDATE_ID"] for row in rows]
    marks = ",".join("?" for _ in candidate_ids)
    db_rows = connection.execute(
        f"""
        SELECT c.candidate_id,c.original_description,c.candidate_description,
          c.review_state,c.publication_state,c.validator_status,c.confidence,
          d.site_id,d.asset_number,d.location_code,d.classification_description
        FROM semantic_candidate c JOIN device_identity d ON d.device_id=c.device_id
        WHERE c.candidate_id IN ({marks})
        """, candidate_ids,
    ).fetchall()
    connection.close()
    db_map = {row["candidate_id"]: row for row in db_rows}
    if len(db_map) != 200:
        raise SystemExit(f"Database/sample mismatch: {len(db_map)} of 200")
    for row in rows:
        db_row = db_map[row["CANDIDATE_ID"]]
        if row["ORIGINAL_DESCRIPTION"] != db_row["original_description"] or row["EXISTING_CANDIDATE_DESCRIPTION"] != db_row["candidate_description"]:
            raise SystemExit(f"Sample changed in database: {row['CANDIDATE_ID']}")
        if db_row["review_state"] != "pending" or db_row["publication_state"] != "unpublished":
            raise SystemExit(f"Sample state is no longer pending: {row['CANDIDATE_ID']}")

    judged = [judge(row) for row in rows]
    columns = list(rows[0].keys()) + [
        "AI_DECISION", "AI_CONFIDENCE", "AI_REASON", "AI_GATE", "DIFF_SIGNATURE",
        "RISK_FLAGS", "JUDGE_VERSION", "JUDGMENT_STATUS",
    ]
    with OUTPUT_CSV.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(judged)

    by_decision = Counter(row["AI_DECISION"] for row in judged)
    by_site: dict[str, Counter[str]] = defaultdict(Counter)
    by_signature: Counter[str] = Counter()
    for row in judged:
        by_site[row["SITEID"]][row["AI_DECISION"]] += 1
        by_signature[row["DIFF_SIGNATURE"]] += 1
    summary = {
        "sample_rows": len(judged),
        "by_decision": dict(by_decision),
        "by_site": {site: dict(counts) for site, counts in sorted(by_site.items())},
        "top_diff_signatures": [{"signature": signature, "count": count} for signature, count in by_signature.most_common(20)],
        "accept_candidate_count": by_decision["接受候选"],
        "preserve_original_count": by_decision["保留原文"],
        "needs_review_count": by_decision["需要复核"],
        "judge_version": JUDGE_VERSION,
        "judgment_status": "recommendation_only",
        "source_write": False,
        "formal_publication": False,
        "next_gate": "cluster-level approval and replay required before any publication",
    }
    manifest = {
        "judgment_id": f"ai-judgment-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}",
        "generated_at_utc": utc_now(),
        "input_file": str(INPUT_CSV),
        "input_sha256": sha256(INPUT_CSV),
        "output_file": str(OUTPUT_CSV),
        "output_sha256": sha256(OUTPUT_CSV),
        "judge_version": JUDGE_VERSION,
        "decision_labels": ["保留原文", "接受候选", "需要复核"],
        "judgment_status": "recommendation_only",
        "source_write": False,
        "formal_publication": False,
        "summary": summary,
    }
    SUMMARY_JSON.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    MANIFEST_JSON.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"manifest": str(MANIFEST_JSON), "output": str(OUTPUT_CSV), **summary}, ensure_ascii=False))


if __name__ == "__main__":
    main()
