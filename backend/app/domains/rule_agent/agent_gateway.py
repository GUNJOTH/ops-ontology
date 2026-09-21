"""Model gateway adapters for rule discovery, reasoning and review.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from fastapi import HTTPException

from app.agent.client import (
    RuleAgentCallError,
    invoke_rule_agent_completion,
    parse_rule_agent_json,
    rule_agent_failure_detail,
)
from app.agent.prompts import rule_agent_payload, semantic_reasoning_payload
from app.core.config import RULE_AGENT_MAX_PROPOSALS


def invoke_rule_agent(profile: dict[str, Any]) -> list[dict[str, Any]]:
    try:
        text = invoke_rule_agent_completion(
            [
                {"role": "system", "content": "Use exact localRuleCatalog patternKey values, or an exact contextPatternKeys value when semanticClusters provide repeated evidence. Never return placeholders such as context.stable-name. Context semantic proposals must use replace only with explicit parameters.from/to and scope. Never invent regex or unsupported parameters. Return JSON only."},
                {"role": "system", "content": "你是电厂设备描述规则发现智能体。必须只输出一个 JSON 对象，格式为 {\"proposals\":[...]}，不要输出解释、前后缀或 Markdown。"},
                {"role": "user", "content": rule_agent_payload(profile)},
            ],
            "规则智能体",
        )
        parsed = parse_rule_agent_json(text, "规则智能体")
    except RuleAgentCallError as exc:
        status = 503 if exc.code == "agent_not_configured" else 502
        raise HTTPException(status_code=status, detail=rule_agent_failure_detail(exc)) from exc
    if isinstance(parsed, dict):
        proposals = parsed.get("proposals", [])
    elif isinstance(parsed, list):
        proposals = parsed
    else:
        proposals = []
    if not isinstance(proposals, list):
        raise HTTPException(status_code=502, detail="规则智能体返回格式不正确")
    return [item for item in proposals if isinstance(item, dict)][:RULE_AGENT_MAX_PROPOSALS]



def invoke_semantic_reasoning_agent(clusters: list[dict[str, Any]]) -> list[dict[str, Any]]:
    try:
        text = invoke_rule_agent_completion(
            [
                {
                    "role": "system",
                    "content": "You are a conservative semantic reasoning agent for power-plant equipment descriptions. Return JSON only. Reason about clusters, never modify source data.",
                },
                {"role": "user", "content": semantic_reasoning_payload(clusters)},
            ],
            "语义推理智能体",
        )
        parsed = parse_rule_agent_json(text, "语义推理智能体")
    except RuleAgentCallError as exc:
        status = 503 if exc.code == "agent_not_configured" else 502
        raise HTTPException(status_code=status, detail=rule_agent_failure_detail(exc)) from exc
    items = parsed.get("reasoning", []) if isinstance(parsed, dict) else parsed
    if not isinstance(items, list):
        raise HTTPException(status_code=502, detail="语义推理智能体返回结构不正确")
    return [item for item in items if isinstance(item, dict)][:30]



def invoke_rule_agent_review(proposals: list[sqlite3.Row]) -> list[dict[str, Any]]:
    """Ask the model to review rule proposals, never individual source rows."""
    review_items = []
    for row in proposals:
        review_items.append(
            {
                "proposalId": row["proposal_id"],
                "ruleKey": row["rule_key"],
                "title": row["title"],
                "objective": row["objective"],
                "operation": row["operation"],
                "condition": json.loads(row["condition_json"] or "{}"),
                "parameters": json.loads(row["parameters_json"] or "{}"),
                "scope": json.loads(row["scope_json"] or "{}"),
                "evidence": json.loads(row["evidence_json"] or "{}"),
                "examples": json.loads(row["examples_json"] or "[]")[:5],
                "expectedCount": int(row["expected_count"] or 0),
                "confidence": float(row["confidence"] or 0),
                "riskLevel": row["risk_level"],
            }
        )
    payload = {
        "task": "审核设备描述统一语义规则草案，只审核规则，不审核或改写单条设备数据",
        "outputSchema": {
            "reviews": [
                {
                    "proposalId": "",
                    "decision": "accept_rule|needs_review|reject",
                    "confidence": 0.0,
                    "reason": "",
                    "requiredChecks": [],
                }
            ]
        },
        "policy": [
            "只允许可解释的确定性规则",
            "不得补造制造商、型号、容量或设备类型",
            "低风险、证据充分、可回放的规则才可建议通过",
            "复杂语义、分类冲突、位置冲突和证据不足必须需要复核",
        ],
        "proposals": review_items,
    }
    try:
        text = invoke_rule_agent_completion(
            [
                {"role": "system", "content": "你是电厂设备语义规则审核智能体。只输出合法 JSON，不输出 Markdown 或解释文字。"},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            "规则审核智能体",
        )
        parsed = parse_rule_agent_json(text, "规则审核智能体")
    except RuleAgentCallError as exc:
        status = 503 if exc.code == "agent_not_configured" else 502
        raise HTTPException(status_code=status, detail=rule_agent_failure_detail(exc)) from exc
    reviews = parsed.get("reviews", []) if isinstance(parsed, dict) else parsed
    return [item for item in reviews if isinstance(item, dict)] if isinstance(reviews, list) else []
