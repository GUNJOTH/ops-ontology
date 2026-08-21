"""Ask the configured rule agent for conservative per-cluster rule drafts."""
from __future__ import annotations

import argparse
import csv
import json
import os
import pathlib
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone

from safe_convert import to_float, to_int


def parse_json(text: str) -> object:
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise
        return json.loads(text[start : end + 1])


def compact_cluster(row: dict[str, str]) -> dict[str, object]:
    try:
        examples = json.loads(row.get("examples_json", "[]"))
    except json.JSONDecodeError:
        examples = []
    return {
        "clusterId": row.get("cluster_id", ""),
        "sourceSchema": row.get("source_schema", ""),
        "count": to_int(row.get("count", "0") or 0),
        "siteCount": to_int(row.get("site_count", "0") or 0),
        "patternSignature": row.get("pattern_signature", ""),
        "patternReadable": row.get("pattern_readable", ""),
        "examples": examples[:4],
    }


def request_batch(base_url: str, api_key: str, model: str, batch: list[dict[str, object]], max_tokens: int, timeout: float, brief_output: bool) -> list[dict[str, object]]:
    payload = {
        "task": "为设备名称语义变化簇生成保守的规则草案。只判断规则，不改写单条设备数据。",
        "policy": [
            "只能使用输入簇中的变化证据和样本，不能推断设备类型、厂家、规格或业务含义。",
            "同一条规则必须限定 sourceSchema；不能跨 HD_SAAS/XNY_SAAS 自动合并。",
            "只允许明确的 replace from/to；禁止正则、模糊匹配和大范围词语替换。",
            "如果只是罗马数字、全角符号等字符规范化，也必须输出 needs_review 或 propose_rule，并说明需人工确认语义不变。",
            "证据不足时输出 keep_original 或 needs_review，不要为了增加规则而提议改写。",
            "所有结果都是草案，必须经过本地回放、全量预览和人工确认，绝不发布。",
        ],
        "output_schema": {
            "drafts": [
                {
                    "clusterId": "exact input clusterId",
                    "decision": "propose_rule|keep_original|needs_review",
                    "title": "short title",
                    "objective": "evidence-based objective",
                    "operation": "replace|keep_original",
                    "parameters": {"from": "", "to": ""},
                    "scope": {"sourceSchema": "", "sites": []},
                    "evidence": {"observations": [], "supportingExamples": [], "counterexamples": []},
                    "confidence": 0.0,
                    "riskLevel": "low|medium|high",
                    "requiredChecks": [],
                }
            ]
        },
        "clusters": batch,
    }
    if brief_output:
        payload["policy"] = [
            "每个 clusterId 必须返回一条结果。",
            "只允许 propose_rule、keep_original、needs_review；证据不足就 needs_review。",
            "propose_rule 必须给出明确 from/to，不能使用正则或模糊替换。",
            "只输出 JSON，不解释，不发布。",
        ]
        payload["output_schema"] = {
            "drafts": [{
                "clusterId": "exact input clusterId",
                "decision": "propose_rule|keep_original|needs_review",
                "parameters": {"from": "", "to": ""},
                "confidence": 0.0,
                "reason": "one short evidence-based sentence",
            }]
        }
    body = json.dumps({
        "model": model,
        "max_tokens": max_tokens,
        "temperature": 0,
        "thinking": {"type": os.getenv("RULE_AGENT_THINKING", "disabled")},
        "messages": [
            {"role": "system", "content": "You are a conservative power-plant equipment semantic-rule analyst. Return JSON only."},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
    }, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=body,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            completion = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"agent_http_{exc.code}:{detail}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(f"agent_connection:{type(exc).__name__}") from exc
    choices = completion.get("choices", []) if isinstance(completion, dict) else []
    message = choices[0].get("message", {}) if choices else {}
    text = message.get("content", "") if isinstance(message, dict) else ""
    if not text:
        keys = ",".join(sorted(message.keys())) if isinstance(message, dict) else "none"
        finish = choices[0].get("finish_reason", "") if choices and isinstance(choices[0], dict) else ""
        raise RuntimeError(f"agent_empty_content:message_keys={keys}:finish_reason={finish}")
    parsed = parse_json(text)
    items = parsed.get("drafts", []) if isinstance(parsed, dict) else parsed
    return items if isinstance(items, list) else []


def validate_items(items: list[dict[str, object]], allowed: set[str]) -> tuple[list[dict[str, object]], list[str]]:
    valid: list[dict[str, object]] = []
    errors: list[str] = []
    seen: set[str] = set()
    for item in items:
        cid = str(item.get("clusterId") or "")
        if cid not in allowed:
            errors.append(f"unknown_cluster:{cid}")
            continue
        if cid in seen:
            errors.append(f"duplicate_cluster:{cid}")
            continue
        seen.add(cid)
        decision = str(item.get("decision") or "needs_review")
        if decision not in {"propose_rule", "keep_original", "needs_review"}:
            errors.append(f"invalid_decision:{cid}")
            decision = "needs_review"
        item["decision"] = decision
        confidence = item.get("confidence", 0)
        try:
            item["confidence"] = max(0.0, min(1.0, float(confidence)))
        except (TypeError, ValueError):
            item["confidence"] = 0.0
        if decision == "propose_rule":
            parameters = item.get("parameters") or {}
            if not isinstance(parameters, dict) or not parameters.get("from") or not parameters.get("to"):
                errors.append(f"proposal_missing_explicit_from_to:{cid}")
                item["decision"] = "needs_review"
        valid.append(item)
    return valid, errors


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--clusters-file", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--batch-size", type=int, default=20)
    parser.add_argument("--max-batches", type=int, default=0, help="仅用于连通性测试；0 表示处理全部批次")
    parser.add_argument("--brief-output", action="store_true", help="只让智能体返回最小规则决策，适合全量簇批处理")
    args = parser.parse_args()
    clusters_path = pathlib.Path(args.clusters_file).resolve()
    output_dir = pathlib.Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=False)
    with clusters_path.open("r", encoding="utf-8-sig", newline="") as handle:
        clusters = [compact_cluster(row) for row in csv.DictReader(handle)]

    base_url = os.getenv("RULE_AGENT_BASE_URL", "").rstrip("/")
    api_key = os.getenv("RULE_AGENT_API_KEY", "")
    model = os.getenv("RULE_AGENT_MODEL", "deepseek-v4-pro")
    max_tokens = to_int(os.getenv("RULE_AGENT_MAX_TOKENS", "4096"), 4096)
    if not base_url or not api_key:
        raise SystemExit("RULE_AGENT_BASE_URL/RULE_AGENT_API_KEY 未配置，未调用外部智能体")
    if not base_url.endswith("/v1"):
        base_url += "/v1"
    timeout = to_float(os.getenv("RULE_AGENT_TIMEOUT", "180"), 180.0)

    drafts: list[dict[str, object]] = []
    errors: list[str] = []
    batches = [clusters[i : i + args.batch_size] for i in range(0, len(clusters), args.batch_size)]
    if args.max_batches > 0:
        batches = batches[:args.max_batches]
    for index, batch in enumerate(batches, start=1):
        allowed = {str(cluster["clusterId"]) for cluster in batch}
        try:
            raw_items = request_batch(base_url, api_key, model, batch, max_tokens, timeout, args.brief_output)
            valid_items, validation_errors = validate_items(raw_items, allowed)
            errors.extend(f"batch_{index}:{item}" for item in validation_errors)
            returned = {str(item.get("clusterId")) for item in valid_items}
            for cluster in batch:
                cid = str(cluster["clusterId"])
                if cid not in returned:
                    valid_items.append({
                        "clusterId": cid,
                        "decision": "needs_review",
                        "title": "agent did not return a complete draft",
                        "objective": "保留原文，等待复核",
                        "operation": "keep_original",
                        "parameters": {"from": "", "to": ""},
                        "scope": {"sourceSchema": cluster["sourceSchema"], "sites": []},
                        "evidence": {"observations": [], "supportingExamples": [], "counterexamples": []},
                        "confidence": 0.0,
                        "riskLevel": "high",
                        "requiredChecks": ["agent response completeness"],
                    })
            for item in valid_items:
                item["batchIndex"] = index
            drafts.extend(valid_items)
            print(json.dumps({"batch": index, "batches": len(batches), "clusters": len(batch), "drafts": len(valid_items)}, ensure_ascii=False))
        except Exception as exc:
            errors.append(f"batch_{index}:{type(exc).__name__}:{exc}")
            for cluster in batch:
                drafts.append({
                    "clusterId": cluster["clusterId"],
                    "decision": "needs_review",
                    "title": "agent call failed",
                    "objective": "保留原文，等待复核",
                    "operation": "keep_original",
                    "parameters": {"from": "", "to": ""},
                    "scope": {"sourceSchema": cluster["sourceSchema"], "sites": []},
                    "evidence": {"observations": [], "supportingExamples": [], "counterexamples": []},
                    "confidence": 0.0,
                    "riskLevel": "high",
                    "requiredChecks": ["agent retry"],
                    "batchIndex": index,
                })

    draft_path = output_dir / "agent_rule_drafts.json"
    draft_path.write_text(json.dumps(drafts, ensure_ascii=False, indent=2), encoding="utf-8")
    result = {
        "run_id": f"semantic-agent-rule-drafts-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}",
        "clusters_file": str(clusters_path),
        "cluster_count": len(clusters),
        "draft_count": len(drafts),
        "decision_counts": {
            decision: sum(item.get("decision") == decision for item in drafts)
            for decision in ("propose_rule", "keep_original", "needs_review")
        },
        "batch_count": len(batches),
        "model": model,
        "draft_file": draft_path.name,
        "errors": errors,
        "source_write": False,
        "formal_publication": False,
        "next_gate": "validate each proposed rule locally, replay, preview, and human approval",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    (output_dir / "manifest.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
