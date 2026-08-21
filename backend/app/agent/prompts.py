"""Stable prompts and bounded payloads for rule discovery agents."""
from __future__ import annotations

import json
from typing import Any

from app.core.config import RULE_AGENT_MAX_PROPOSALS


def rule_agent_payload(profile: dict[str, Any]) -> str:
    compact_profile = dict(profile)
    compact_profile["examples"] = profile.get("examples", [])[:5]
    compact_profile["sites"] = profile.get("sites", [])[:20]
    compact_profile["classifications"] = profile.get("classifications", [])[:20]
    compact_profile["localRuleCatalog"] = [
        {**item, "examples": item.get("examples", [])[:3]}
        for item in profile.get("localRuleCatalog", [])
    ]
    compact_profile["blockedRuleCatalog"] = profile.get("blockedRuleCatalog", [])[:10]
    compact_profile["semanticClusters"] = [
        {**item, "examples": item.get("examples", [])[:3], "descriptions": item.get("descriptions", [])[:6]}
        for item in profile.get("semanticClusters", [])[:30]
    ]
    compact_profile["contextPatternKeys"] = [
        item.get("patternKey") for item in compact_profile["semanticClusters"] if item.get("patternKey")
    ]
    return json.dumps(
        {
            "task": f"从设备描述数据中发现最多 {RULE_AGENT_MAX_PROPOSALS} 条可重复、可解释、可回放的清洗规则",
            "policy": [
                "只能选择 localRuleCatalog.patternKey 或 contextPatternKeys 中的精确值，不能编造占位符。",
                "上下文规则只能使用显式 parameters.from/to 的 replace；normalize 和 trim 仅限本地格式目录。",
                "本地执行器会丢弃零命中或无样本证据的草案；所有结果只是草案。",
                "AI 只提供标题、目标、理由、置信度和风险评估，禁止修改源数据或发布。",
            ],
            "output_schema": {
                "proposals": [
                    {
                        "patternKey": "local.normalize.whitespace|context.cluster.<exact-key>",
                        "ruleKey": "stable.dot.separated.key",
                        "title": "规则名称",
                        "objective": "规则目的",
                        "operation": "replace|normalize|trim|keep_original|block",
                        "condition": {"descriptionPattern": "可解释条件", "context": "可选上下文条件"},
                        "parameters": {"from": "", "to": ""},
                        "scope": {"sites": [], "classifications": [], "locationParents": [], "locationCodes": [], "kksPrefixes": []},
                        "evidence": {"reason": "", "matchedCount": 0},
                        "examples": [{"before": "", "after": "", "candidateId": ""}],
                        "expectedCount": 0,
                        "confidence": 0.0,
                        "riskLevel": "low|medium|high",
                    }
                ]
            },
            "profile": compact_profile,
        },
        ensure_ascii=False,
    )


def semantic_reasoning_payload(clusters: list[dict[str, Any]]) -> str:
    compact_clusters = [
        {**cluster, "examples": cluster.get("examples", [])[:4], "descriptions": cluster.get("descriptions", [])[:8]}
        for cluster in clusters
    ]
    return json.dumps(
        {
            "task": "Reason over equipment-description clusters and context; do not rewrite individual records.",
            "policy": [
                "只使用簇中的描述、KKS/位置父级、分类、电厂和样本证据。",
                "没有明确证据时不得推断厂家、型号、容量、设备类型或标准术语。",
                "候选规则必须是显式 replace from/to，不得使用正则或宽泛归一化。",
                "只有合法编号、位置序号或身份差异时返回 keep_original 或 needs_review。",
                "每条规则都必须经过本地命中、回放、预览和人工确认，禁止直接发布。",
            ],
            "output_schema": {
                "reasoning": [
                    {
                        "clusterKey": "context.cluster.<exact-key>",
                        "decision": "propose_rule|keep_original|needs_review",
                        "hypothesis": "short evidence-based explanation",
                        "evidence": {"observations": [], "supportingExamples": []},
                        "counterexamples": [],
                        "candidateRule": {
                            "ruleKey": "context.cluster.<exact-key>.term",
                            "operation": "replace",
                            "condition": {"contains": ""},
                            "parameters": {"from": "", "to": ""},
                            "scope": {"sites": [], "classifications": [], "locationParents": [], "locationCodes": [], "kksPrefixes": []},
                        },
                        "confidence": 0.0,
                        "riskLevel": "medium|high",
                        "requiredChecks": [],
                    }
                ]
            },
            "clusters": compact_clusters,
        },
        ensure_ascii=False,
    )

