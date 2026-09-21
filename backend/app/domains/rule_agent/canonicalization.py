"""Deterministic canonicalization for semantic reasoning responses.
"""
from __future__ import annotations

import json
import re
import sqlite3
from typing import Any


def canonicalize_semantic_reasoning(
    items: list[dict[str, Any]],
    clusters: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    cluster_aliases: dict[str, str] = {}
    for cluster in clusters:
        cluster_key = str(cluster.get("clusterKey") or "")
        if not cluster_key:
            continue
        cluster_aliases[cluster_key] = cluster_key
        cluster_aliases[f"context.cluster.{cluster_key}"] = cluster_key
        if cluster.get("patternKey"):
            cluster_aliases[str(cluster["patternKey"])] = cluster_key
    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        raw_cluster_key = str(item.get("clusterKey") or item.get("clusterId") or "").strip()
        cluster_key = cluster_aliases.get(raw_cluster_key, "")
        if not cluster_key or cluster_key in seen:
            continue
        seen.add(cluster_key)
        decision = str(item.get("decision") or "needs_review").strip().lower()
        decision = {
            "propose": "propose_rule",
            "propose_rule": "propose_rule",
            "keep": "keep_original",
            "preserve": "keep_original",
            "keep_original": "keep_original",
            "review": "needs_review",
            "needs_review": "needs_review",
        }.get(decision, "needs_review")
        if decision not in {"propose_rule", "keep_original", "needs_review"}:
            decision = "needs_review"
        try:
            confidence = max(0.0, min(1.0, float(item.get("confidence") or 0)))
        except (TypeError, ValueError):
            confidence = 0.0
        risk = str(item.get("riskLevel") or "medium").strip().lower()
        if risk not in {"low", "medium", "high"}:
            risk = "medium"
        if decision == "propose_rule":
            risk = "high" if risk == "high" else "medium"
        candidate = item.get("candidateRule") or item.get("ruleDraft") or item.get("rule")
        candidate = candidate if isinstance(candidate, dict) else {}
        operation = str(candidate.get("operation") or "").strip().lower()
        condition = candidate.get("condition") if isinstance(candidate.get("condition"), dict) else {}
        parameters = candidate.get("parameters") if isinstance(candidate.get("parameters"), dict) else {}
        scope = candidate.get("scope") if isinstance(candidate.get("scope"), dict) else {}
        required_checks = item.get("requiredChecks") if isinstance(item.get("requiredChecks"), list) else []
        required_checks = [str(value)[:300] for value in required_checks[:20]]
        if decision == "propose_rule":
            source = str(parameters.get("from") or "")
            target = str(parameters.get("to") if parameters.get("to") is not None else "")
            allowed_condition = {"contains", "prefix", "suffix"}
            allowed_scope = {"sites", "classifications", "locationParents", "locationCodes", "kksPrefixes"}
            valid = (
                operation == "replace"
                and bool(source)
                and source != target
                and len(source) <= 80
                and len(target) <= 80
                and all(key in allowed_condition for key in condition)
                and all(key in allowed_scope for key in scope)
            )
            if not valid:
                decision = "needs_review"
                required_checks.append("candidate_rule_dsl_invalid")
            else:
                candidate = {
                    "ruleKey": str(candidate.get("ruleKey") or f"context.{cluster_key}.term")[:180],
                    "operation": "replace",
                    "condition": condition,
                    "parameters": {"from": source, "to": target},
                    "scope": scope,
                }
        evidence = item.get("evidence") if isinstance(item.get("evidence"), dict) else {}
        if isinstance(item.get("evidence"), str):
            evidence = {"reason": str(item["evidence"])[:2000]}
        counterexamples = item.get("counterexamples") if isinstance(item.get("counterexamples"), list) else []
        output.append(
            {
                "clusterKey": cluster_key,
                "decision": decision,
                "hypothesis": str(item.get("hypothesis") or "")[:2000],
                "evidence": evidence,
                "counterexamples": counterexamples[:20],
                "candidateRule": candidate if decision == "propose_rule" else {},
                "confidence": confidence,
                "riskLevel": risk,
                "requiredChecks": required_checks,
            }
        )
    return output



def semantic_reasoning_item_payload(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "reasoningItemId": row["reasoning_item_id"],
        "runId": row["reasoning_run_id"],
        "clusterKey": row["cluster_key"],
        "decision": row["decision"],
        "hypothesis": row["hypothesis"],
        "evidence": json.loads(row["evidence_json"] or "{}"),
        "counterexamples": json.loads(row["counterexamples_json"] or "[]"),
        "candidateRule": json.loads(row["candidate_rule_json"] or "{}"),
        "confidence": float(row["confidence"] or 0),
        "riskLevel": row["risk_level"],
        "requiredChecks": json.loads(row["required_checks_json"] or "[]"),
        "createdAt": row["created_at"],
    }



def context_rule_spec_from_agent(item: dict[str, Any], pattern_key: str) -> dict[str, Any] | None:
    """Validate a context proposal before it reaches the local executor."""
    if not re.fullmatch(r"context\.[a-z0-9][a-z0-9_.-]{2,120}", pattern_key):
        return None
    operation = str(item.get("operation") or "").strip().lower()
    if operation != "replace":
        return None
    condition = item.get("condition") if isinstance(item.get("condition"), dict) else {}
    allowed_conditions = {"contains", "descriptionContains", "prefix", "suffix"}
    if any(key not in allowed_conditions for key in condition):
        return None
    parameters = item.get("parameters") if isinstance(item.get("parameters"), dict) else {}
    if operation == "replace":
        source = str(parameters.get("from") or parameters.get("source") or "")
        if not source or len(source) > 80:
            return None
        target = str(parameters.get("to") if parameters.get("to") is not None else parameters.get("target") or "")
        if len(target) > 80 or source == target:
            return None
        parameters = {"from": source, "to": target}
    elif operation == "normalize":
        mode = str(parameters.get("mode") or "").strip().lower()
        if mode not in {"nfkc", "whitespace", "nfkc_whitespace"}:
            return None
        parameters = {"mode": mode}
    else:
        parameters = {}
    raw_scope = item.get("scope") if isinstance(item.get("scope"), dict) else {}
    allowed_scope = {"sites", "siteIds", "classifications", "classificationIds", "locationParents", "locationParent", "locationCodes", "locationCode", "kksPrefixes", "kksPrefix"}
    if any(key not in allowed_scope for key in raw_scope):
        return None
    scope = {key: value for key, value in raw_scope.items() if value not in (None, [], "")}
    if not condition and not scope:
        return None
    return {
        "patternKey": pattern_key,
        "ruleKey": re.sub(r"[^a-z0-9_.-]+", "_", str(item.get("ruleKey") or pattern_key).lower())[:180],
        "title": str(item.get("title") or pattern_key)[:200],
        "objective": str(item.get("objective") or "Context-scoped semantic rule draft")[:1000],
        "operation": operation,
        "condition": condition,
        "parameters": parameters,
        "scope": scope,
        "riskLevel": "medium",
        "confidence": 0.80,
        "matchedCount": 0,
        "sampleCount": 0,
        "examples": [],
        "evidenceSource": "semantic_context_cluster_agent",
        "catalogVersion": "semantic-context-dsl-20260814-v1",
    }
