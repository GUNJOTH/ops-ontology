"""Deterministic candidate-difference clustering rules.

The module is intentionally persistence-free.  It describes and groups a
candidate difference, but never changes review, approval, or publication
state.  Database loading and decision writes remain in ``service.py``.
"""
from __future__ import annotations

import hashlib
import re
from collections import Counter
from typing import Any

AI_DECISION_KEYS = ("keep_original", "accept_candidate", "needs_review")


def cluster_pattern(kind: str, original: str, candidate: str) -> str:
    """Return a stable, explainable rule signature for a changed description."""
    value = original
    if kind == "leading_minus" and value.startswith("-"):
        value = value[1:]
    if kind == "terminal_hyphen" and value.endswith("-"):
        value = value[:-1]
    value = value.replace("？", "?")
    value = re.sub(r"\d+(?:\.\d+)?", "{N}", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value or "<empty>"


def classify_ai_cluster(original: str, candidate: str) -> tuple[str, str, str, float, str]:
    """Classify a difference conservatively without changing candidate state."""
    if original.replace("？", "?") == candidate and original != candidate:
        return (
            "question_context",
            "问号上下文",
            "needs_review",
            0.88,
            "问号可能表达相位、占位或未知字段，先按簇确认，不自动删除。",
        )
    if original.startswith("-") and original[1:] == candidate:
        return (
            "leading_minus",
            "前导负号",
            "keep_original",
            0.99,
            "删除前导负号可能改变负标高、负电压或负值含义，默认保留原文。",
        )
    if original.endswith("-") and original[:-1] == candidate:
        return (
            "terminal_hyphen",
            "末尾横线",
            "needs_review",
            0.83,
            "末尾横线可能是孤立标记，也可能属于设备命名习惯，需按簇确认。",
        )
    return (
        "other",
        "其他差异",
        "needs_review",
        0.60,
        "差异未命中当前确定性规则，保留人工复核入口。",
    )


def ai_cluster_id(kind: str, pattern: str) -> str:
    digest = hashlib.sha256(f"remaining-diff-v1|{kind}|{pattern}".encode("utf-8")).hexdigest()[:20]
    return f"cluster-{digest}"


def summarize_ai_clusters(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Build stable cluster summaries from already-loaded candidate rows."""
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(row["clusterId"], []).append(row)
    clusters: list[dict[str, Any]] = []
    suggestion_counts = {key: 0 for key in AI_DECISION_KEYS}
    decision_counts = {key: 0 for key in AI_DECISION_KEYS}
    reviewed_candidates = 0
    site_counts = Counter((row.get("siteId") or "").strip() or "未填写" for row in rows)
    classification_counts = Counter((row.get("classificationDescription") or "").strip() or "未分类" for row in rows)
    for cluster_id, members in grouped.items():
        members.sort(key=lambda item: (item["siteId"], item["assetNumber"], item["candidateId"]))
        first = members[0]
        cluster_decision = first["clusterDecision"]
        reviewed_count = sum(1 for item in members if item["memberDecision"])
        reviewed_candidates += reviewed_count
        suggestion_counts[first["aiDecision"]] += 1
        if cluster_decision in decision_counts:
            decision_counts[cluster_decision] += 1
        sites: dict[str, int] = {}
        for item in members:
            sites[item["siteId"]] = sites.get(item["siteId"], 0) + 1
        clusters.append({
            "clusterId": cluster_id,
            "clusterType": first["clusterType"],
            "clusterLabel": first["clusterLabel"],
            "clusterPattern": first["clusterPattern"],
            "ruleSignature": first["ruleSignature"],
            "memberCount": len(members),
            "reviewedCount": reviewed_count,
            "pendingCount": len(members) - reviewed_count,
            "siteIds": sorted(sites),
            "sites": [{"siteId": site_id, "count": count} for site_id, count in sorted(sites.items())],
            "aiDecision": first["aiDecision"],
            "aiConfidence": first["aiConfidence"],
            "aiReason": first["aiReason"],
            "decision": cluster_decision,
            "sample": {key: first[key] for key in ("candidateId", "siteId", "assetNumber", "originalDescription", "candidateDescription", "kks", "locationDescription", "locationParent", "classificationDescription")},
        })
    clusters.sort(key=lambda item: (-item["memberCount"], item["clusterType"], item["clusterId"]))
    summary = {
        "candidateCount": len(rows),
        "clusterCount": len(clusters),
        "pendingCandidateCount": len(rows) - reviewed_candidates,
        "reviewedCandidateCount": reviewed_candidates,
        "pendingClusterCount": sum(1 for item in clusters if item["decision"] is None),
        "aiRecommendation": suggestion_counts,
        "clusterDecision": decision_counts,
        "sites": [{"siteId": key, "count": count} for key, count in sorted(site_counts.items(), key=lambda item: (-item[1], item[0]))],
        "classifications": [{"value": key, "count": count} for key, count in sorted(classification_counts.items(), key=lambda item: (-item[1], item[0]))[:50]],
    }
    return clusters, summary
