"""Rule agent and semantic reasoning routes (native APIRouter)."""
from __future__ import annotations

import csv
import hashlib
import json
import re
import sqlite3
import unicodedata
from pathlib import Path
from typing import Any

from fastapi import HTTPException
from semantic_lib import sha256_file

from app.agent.client import (
    RuleAgentCallError,
    invoke_rule_agent_completion,
    parse_rule_agent_json,
    rule_agent_failure_detail,
)
from app.agent.prompts import rule_agent_payload, semantic_reasoning_payload
from app.core.config import (
    RULE_AGENT_CATALOG_VERSION,
    RULE_AGENT_DIR,
    RULE_AGENT_MAX_PROPOSALS,
)
from app.core.utils import utc_now
from app.domains.candidates.service import latest_batch

from .engine import rule_agent_scope_matches, rule_agent_stratified_sample, rule_agent_transform

_SEMANTIC_CONTEXT_CLUSTER_CACHE: dict[tuple[str, int, int], list[dict[str, Any]]] = {}


RULE_AGENT_EVIDENCE_SQL = "c.evidence_level IN ('strong', 'source_preview_and_replay')"
RULE_AGENT_CONTEXT_SAMPLE_LIMIT = 10000


RULE_AGENT_CONTEXT_SAMPLE_LIMIT = 10000



def rule_agent_profile(connection: sqlite3.Connection, sample_size: int = 120) -> dict[str, Any]:
    """Build a compact, source-read-only profile for the rule discovery agent."""
    batch = latest_batch(connection)
    eligible_where = f"""
        c.batch_id=? AND c.confidence='high' AND {RULE_AGENT_EVIDENCE_SQL}
        AND c.validator_status='candidate'
        AND c.review_state='pending' AND c.publication_state='unpublished'
        AND length(trim(c.original_description)) > 0
    """
    eligible_count = int(connection.execute(f"SELECT count(*) FROM semantic_candidate c WHERE {eligible_where}", (batch["batch_id"],)).fetchone()[0])
    changed_count = int(connection.execute(f"SELECT count(*) FROM semantic_candidate c WHERE {eligible_where} AND c.original_description<>c.candidate_description", (batch["batch_id"],)).fetchone()[0])
    # Read one deterministic pool, then stratify it in memory.  The previous
    # implementation issued one full candidate query per site, which made the
    # profile endpoint unusable for the current 397k-row batch.
    pool_limit = min(max(sample_size * 20, 2000), 10000)
    sample_pool = connection.execute(
        f"""
        SELECT c.candidate_id,c.original_description,c.candidate_description,
          d.site_id,d.asset_number,d.location_code,d.location_description,
          d.location_parent,d.classification_description
        FROM semantic_candidate c JOIN device_identity d ON d.device_id=c.device_id
        WHERE {eligible_where}
        ORDER BY c.candidate_id
        LIMIT ?
        """,
        (batch["batch_id"], pool_limit),
    ).fetchall()
    by_site: dict[str, list[sqlite3.Row]] = {}
    for row in sample_pool:
        by_site.setdefault(row["site_id"] or "", []).append(row)
    selected_rows: list[sqlite3.Row] = []
    if by_site:
        quota = max(1, sample_size // len(by_site))
        for site_id in sorted(by_site):
            selected_rows.extend(by_site[site_id][:quota])
        selected_ids = {row["candidate_id"] for row in selected_rows}
        if len(selected_rows) < sample_size:
            selected_rows.extend(
                row for row in sample_pool
                if row["candidate_id"] not in selected_ids
            )
    selected = selected_rows[:sample_size]
    sites = connection.execute(
        f"""
        SELECT d.site_id,count(*) AS count
        FROM semantic_candidate c JOIN device_identity d ON d.device_id=c.device_id
        WHERE c.batch_id=? AND c.confidence='high' AND {RULE_AGENT_EVIDENCE_SQL}
          AND c.validator_status='candidate'
          AND c.review_state='pending' AND c.publication_state='unpublished'
        GROUP BY d.site_id ORDER BY count DESC
        """,
        (batch["batch_id"],),
    ).fetchall()
    classifications = connection.execute(
        f"""
        SELECT COALESCE(NULLIF(trim(d.classification_description),''),'未分类') AS value,count(*) AS count
        FROM semantic_candidate c JOIN device_identity d ON d.device_id=c.device_id
        WHERE c.batch_id=? AND c.confidence='high' AND {RULE_AGENT_EVIDENCE_SQL}
          AND c.review_state='pending' AND c.publication_state='unpublished'
        GROUP BY value ORDER BY count DESC LIMIT 30
        """,
        (batch["batch_id"],),
    ).fetchall()
    examples = []
    for row in selected:
        examples.append({
            "candidateId": row["candidate_id"],
            "siteId": row["site_id"] or "",
            "assetNumber": row["asset_number"] or "",
            "originalDescription": row["original_description"] or "",
            "candidateDescription": row["candidate_description"] or "",
            "kks": row["location_code"] or "",
            "locationDescription": row["location_description"] or "",
            "locationParent": row["location_parent"] or "",
            "classificationDescription": row["classification_description"] or "",
        })
    local_rule_catalog, blocked_rule_catalog = build_rule_agent_local_catalog(
        connection,
        batch["batch_id"],
        max(6, RULE_AGENT_MAX_PROPOSALS * 4),
    )
    semantic_clusters = build_semantic_context_clusters(connection, batch["batch_id"], limit=40)
    return {
        "batchId": batch["batch_id"],
        "sourceSnapshotId": batch["source_snapshot_id"],
        "eligibleCount": eligible_count,
        "changedCount": changed_count,
        "sampleCount": len(examples),
        "sites": [{"siteId": row["site_id"], "count": int(row["count"])} for row in sites],
        "classifications": [{"value": row["value"], "count": int(row["count"])} for row in classifications],
        "examples": examples,
        "semanticClusters": semantic_clusters,
        "localRuleCatalog": local_rule_catalog,
        "blockedRuleCatalog": blocked_rule_catalog,
        "constraints": [
            "只提出设备描述清洗或统一语义规则",
            "不得臆造制造商、型号、容量或设备属性",
            "规则必须可确定性执行并可回放验证",
            "不得修改源库、候选记录或正式结果",
        ],
    }


def semantic_description_skeleton(value: str) -> str:
    """Create a grouping key without making semantic claims about a value."""
    text = unicodedata.normalize("NFKC", value or "")
    text = re.sub(r"[0-9０-９]+(?:[.．][0-9０-９]+)?", "<NUM>", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def build_semantic_context_clusters(
    connection: sqlite3.Connection,
    batch_id: str,
    limit: int = 40,
) -> list[dict[str, Any]]:
    """Summarize repeated description shapes with KKS/location/class context.

    Clusters are evidence for the agent only.  They do not imply that one
    description is preferred over another, and no cluster mutates a row.
    """
    eligible_count = int(
        connection.execute(
            f"""
            SELECT count(*)
            FROM semantic_candidate c
            WHERE c.batch_id=? AND c.confidence='high' AND {RULE_AGENT_EVIDENCE_SQL}
              AND c.validator_status='candidate' AND c.review_state='pending'
              AND c.publication_state='unpublished'
              AND length(trim(c.original_description)) > 0
            """,
            (batch_id,),
        ).fetchone()[0]
    )
    cache_key = (batch_id, limit, eligible_count)
    cached = _SEMANTIC_CONTEXT_CLUSTER_CACHE.get(cache_key)
    if cached is not None:
        return cached
    rows = connection.execute(
        f"""
        SELECT c.candidate_id,c.original_description,d.site_id,d.asset_number,
          d.location_code,d.location_description,d.location_parent,
          d.classification_description
        FROM semantic_candidate c JOIN device_identity d ON d.device_id=c.device_id
        WHERE c.batch_id=? AND c.confidence='high' AND {RULE_AGENT_EVIDENCE_SQL}
          AND c.validator_status='candidate' AND c.review_state='pending'
          AND c.publication_state='unpublished'
          AND length(trim(c.original_description)) > 0
        ORDER BY c.candidate_id
        LIMIT ?
        """,
        (batch_id, RULE_AGENT_CONTEXT_SAMPLE_LIMIT),
    ).fetchall()
    groups: dict[tuple[str, str], list[sqlite3.Row]] = {}
    for row in rows:
        skeleton = semantic_description_skeleton(row["original_description"] or "")
        classification = (row["classification_description"] or "").strip() or "__UNCLASSIFIED__"
        groups.setdefault((skeleton, classification), []).append(row)

    clusters: list[dict[str, Any]] = []
    for (skeleton, classification), group_members in groups.items():
        if len(group_members) < 2:
            continue
        members = group_members[:8]
        descriptions = list(dict.fromkeys((row["original_description"] or "") for row in members))
        site_ids = sorted({row["site_id"] or "" for row in members})
        parents = sorted({row["location_parent"] or "" for row in members if row["location_parent"]})
        kks_prefixes = sorted({(row["location_code"] or "")[:5] for row in members if row["location_code"]})
        examples = [
            {
                "candidateId": row["candidate_id"],
                "siteId": row["site_id"] or "",
                "assetNumber": row["asset_number"] or "",
                "before": row["original_description"] or "",
                "kks": row["location_code"] or "",
                "locationDescription": row["location_description"] or "",
                "locationParent": row["location_parent"] or "",
                "classificationDescription": row["classification_description"] or "",
            }
            for row in members[:4]
        ]
        clusters.append(
            {
                "clusterKey": hashlib.sha256(f"{skeleton}|{classification}".encode("utf-8")).hexdigest()[:16],
                "memberCount": len(group_members),
                "uniqueDescriptionCount": len({row["original_description"] or "" for row in group_members}),
                "descriptionSkeleton": skeleton,
                "classification": classification,
                "siteIds": site_ids[:20],
                "locationParents": parents[:20],
                "kksPrefixes": kks_prefixes[:20],
                "descriptions": descriptions[:8],
                "examples": examples,
            }
        )
    for item in clusters:
        item["patternKey"] = f"context.cluster.{item['clusterKey']}"
    clusters.sort(key=lambda item: (-item["uniqueDescriptionCount"], -item["memberCount"], item["clusterKey"]))
    result = clusters[:limit]
    _SEMANTIC_CONTEXT_CLUSTER_CACHE[cache_key] = result
    return result


def build_rule_agent_local_catalog(
    connection: sqlite3.Connection,
    batch_id: str,
    limit: int = 12,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Mine only executable, evidence-backed rule patterns from local data."""
    specs = [
        {
            "patternKey": "local.normalize.whitespace",
            "ruleKey": "format.normalize.whitespace",
            "title": "Normalize whitespace",
            "objective": "Collapse repeated, full-width, and boundary whitespace without changing equipment words.",
            "operation": "normalize",
            "condition": {},
            "parameters": {"mode": "whitespace"},
            "riskLevel": "low",
            "confidence": 0.98,
        },
        {
            "patternKey": "local.normalize.nfkc",
            "ruleKey": "format.normalize.nfkc",
            "title": "Normalize full-width and Unicode variants",
            "objective": "Normalize Unicode compatibility variants supported by NFKC.",
            "operation": "normalize",
            "condition": {},
            "parameters": {"mode": "nfkc"},
            "riskLevel": "low",
            "confidence": 0.96,
        },
        {
            "patternKey": "local.trim.boundary-whitespace",
            "ruleKey": "format.trim.boundary_whitespace",
            "title": "Trim boundary whitespace",
            "objective": "Remove whitespace before or after the original description.",
            "operation": "trim",
            "condition": {},
            "parameters": {},
            "riskLevel": "low",
            "confidence": 0.99,
        },
        {
            "patternKey": "local.replace.fullwidth-question-separator",
            "ruleKey": "format.separator.fullwidth_question_to_space",
            "title": "Replace full-width question separator",
            "objective": "Replace an internal full-width question separator with one space.",
            "operation": "replace",
            "condition": {"contains": "？"},
            "parameters": {"from": "？", "to": " "},
            "riskLevel": "medium",
            "confidence": 0.88,
        },
        {
            "patternKey": "local.replace.terminal-hyphen",
            "ruleKey": "format.terminal_hyphen_trim",
            "title": "Trim terminal hyphen",
            "objective": "Remove a trailing hyphen used as an incomplete separator.",
            "operation": "replace",
            "condition": {"suffix": "-"},
            "parameters": {"from": "-", "to": ""},
            "riskLevel": "low",
            "confidence": 0.94,
        },
        {
            "patternKey": "local.replace.uppercase-kv",
            "ruleKey": "format.unit.uppercase_kv_to_kv",
            "title": "Normalize KV unit capitalization",
            "objective": "Use kV for the unit capitalization when the source uses KV.",
            "operation": "replace",
            "condition": {"contains": "KV"},
            "parameters": {"from": "KV", "to": "kV"},
            "riskLevel": "low",
            "confidence": 0.9,
        },
    ]
    spec_by_pattern = {spec["patternKey"]: spec for spec in specs}

    def local_rule_match(value: str, pattern_key: str) -> int:
        spec = spec_by_pattern.get(pattern_key)
        if spec is None:
            return 0
        matched, transformed, _ = rule_agent_transform(
            value or "",
            spec["operation"],
            spec["condition"],
            spec["parameters"],
        )
        return int(matched and transformed != (value or ""))

    connection.create_function("rule_agent_local_match", 2, local_rule_match)
    catalog: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    for spec in specs:
        match_parameters = (batch_id, spec["patternKey"])
        matched_count = int(connection.execute(
            f"""
            SELECT count(*)
            FROM semantic_candidate c
            WHERE c.batch_id=? AND c.confidence='high' AND {RULE_AGENT_EVIDENCE_SQL}
              AND c.validator_status='candidate' AND c.review_state='pending'
              AND c.publication_state='unpublished' AND length(trim(c.original_description)) > 0
              AND rule_agent_local_match(c.original_description, ?)=1
            """,
            match_parameters,
        ).fetchone()[0])
        if matched_count == 0:
            continue
        rows = connection.execute(
            f"""
            SELECT c.candidate_id,c.original_description,d.site_id,d.asset_number,
                   d.location_code,d.location_description,d.location_parent,d.classification_description
            FROM semantic_candidate c JOIN device_identity d ON d.device_id=c.device_id
            WHERE c.batch_id=? AND c.confidence='high' AND {RULE_AGENT_EVIDENCE_SQL}
              AND c.validator_status='candidate' AND c.review_state='pending'
              AND c.publication_state='unpublished' AND length(trim(c.original_description)) > 0
              AND rule_agent_local_match(c.original_description, ?)=1
            ORDER BY c.candidate_id
            LIMIT 5
            """,
            match_parameters,
        ).fetchall()
        targets: list[dict[str, Any]] = []
        for row in rows:
            original = row["original_description"] or ""
            matched, transformed, reason = rule_agent_transform(
                original,
                spec["operation"],
                spec["condition"],
                spec["parameters"],
            )
            if not matched or transformed == original:
                continue
            targets.append(
                {
                    "before": original,
                    "after": transformed,
                    "candidateId": row["candidate_id"],
                    "siteId": row["site_id"] or "",
                    "assetNumber": row["asset_number"] or "",
                    "kks": row["location_code"] or "",
                    "locationDescription": row["location_description"] or "",
                    "locationParent": row["location_parent"] or "",
                    "classificationDescription": row["classification_description"] or "",
                    "matchReason": reason,
                }
            )
        if not targets:
            continue
        catalog_item = {
            **spec,
            "matchedCount": matched_count,
            "sampleCount": len(targets),
            "examples": targets,
            "evidenceSource": "local_candidate_scan",
            "catalogVersion": RULE_AGENT_CATALOG_VERSION,
        }
        replay_gate = evaluate_rule_agent_catalog_spec(connection, catalog_item)
        catalog_item["evaluationCount"] = replay_gate["evaluationCount"]
        catalog_item["evaluationPassCount"] = replay_gate["evaluationPassCount"]
        catalog_item["evaluationFailCount"] = replay_gate["evaluationFailCount"]
        if replay_gate["status"] != "passed":
            blocked.append(
                {
                    "patternKey": spec["patternKey"],
                    "matchedCount": len(targets),
                    "sampleCount": min(5, len(targets)),
                    "reason": "active_evaluation_regression",
                    "evaluationCount": replay_gate["evaluationCount"],
                    "evaluationFailCount": replay_gate["evaluationFailCount"],
                    "failures": replay_gate["failures"],
                }
            )
            continue
        catalog.append(catalog_item)
    catalog.sort(key=lambda item: (-int(item["matchedCount"]), str(item["patternKey"])))
    return catalog[:limit], blocked


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



def canonicalize_rule_agent_proposals(
    proposals: list[dict[str, Any]],
    catalog: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool]:
    """Bind AI output to the locally mined executable rule catalog."""
    catalog_by_key = {str(item["patternKey"]): item for item in catalog}
    selected: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    used: set[str] = set()
    for item in proposals:
        pattern_key = str(item.get("patternKey") or "").strip()
        spec = catalog_by_key.get(pattern_key)
        if spec is None and pattern_key.startswith("context."):
            spec = context_rule_spec_from_agent(item, pattern_key)
        if spec is None:
            rejected.append(
                {
                    "patternKey": pattern_key,
                    "reason": "context_dsl_validation_failed" if pattern_key.startswith("context.") else "unsupported_or_missing_pattern_key",
                    "operation": item.get("operation"),
                    "condition": item.get("condition"),
                    "parameters": item.get("parameters"),
                    "scope": item.get("scope"),
                }
            )
            continue
        if pattern_key in used:
            rejected.append({"patternKey": pattern_key, "reason": "duplicate_pattern_key"})
            continue
        used.add(pattern_key)
        try:
            model_confidence = max(0.0, min(1.0, float(item.get("confidence") or 0)))
        except (TypeError, ValueError):
            model_confidence = 0.0
        risk_order = {"low": 0, "medium": 1, "high": 2}
        model_risk = str(item.get("riskLevel") or spec["riskLevel"]).lower()
        if model_risk not in risk_order:
            model_risk = spec["riskLevel"]
        risk = max((spec["riskLevel"], model_risk), key=lambda value: risk_order[value])
        ai_evidence = item.get("evidence") if isinstance(item.get("evidence"), dict) else {}
        selected.append(
            {
                **item,
                "patternKey": pattern_key,
                "ruleKey": spec["ruleKey"],
                "title": str(item.get("title") or spec["title"])[:200],
                "objective": str(item.get("objective") or spec["objective"])[:1000],
                "operation": spec["operation"],
                "condition": spec["condition"],
                "parameters": spec["parameters"],
                "scope": {},
                "evidence": {
                    "reason": str(ai_evidence.get("reason") or "")[:1000],
                    "matchedCount": spec["matchedCount"],
                    "sampleCount": spec["sampleCount"],
                    "measuredFromLocalPreview": True,
                    "evidenceSource": spec["evidenceSource"],
                    "catalogVersion": spec["catalogVersion"],
                    "patternKey": pattern_key,
                },
                "examples": spec["examples"],
                "expectedCount": spec["matchedCount"],
                "confidence": min(model_confidence, float(spec["confidence"])) if model_confidence else 0.0,
                "riskLevel": risk,
            }
        )

    fallback_used = False
    if not selected:
        fallback_used = bool(catalog)
        for spec in catalog[:RULE_AGENT_MAX_PROPOSALS]:
            selected.append(
                {
                    "patternKey": spec["patternKey"],
                    "ruleKey": spec["ruleKey"],
                    "title": spec["title"],
                    "objective": spec["objective"],
                    "operation": spec["operation"],
                    "condition": spec["condition"],
                    "parameters": spec["parameters"],
                    "scope": {},
                    "evidence": {
                        "reason": "local deterministic catalog fallback",
                        "matchedCount": spec["matchedCount"],
                        "sampleCount": spec["sampleCount"],
                        "measuredFromLocalPreview": True,
                        "evidenceSource": spec["evidenceSource"],
                        "catalogVersion": spec["catalogVersion"],
                        "patternKey": spec["patternKey"],
                    },
                    "examples": spec["examples"],
                    "expectedCount": spec["matchedCount"],
                    "confidence": float(spec["confidence"]),
                    "riskLevel": spec["riskLevel"],
                    "agentGenerated": False,
                }
            )
    return selected[:RULE_AGENT_MAX_PROPOSALS], rejected, fallback_used


AGENT_REVIEW_VERSION = "rule-agent-review-20260814-v1"
RULE_AGENT_DISCOVERY_FILTER_VERSION = "rule-agent-local-evidence-20260814-v1"


RULE_AGENT_DISCOVERY_FILTER_VERSION = "rule-agent-local-evidence-20260814-v1"
RULE_AGENT_MIN_MATCH_COUNT = 1


RULE_AGENT_MIN_MATCH_COUNT = 1
RULE_AGENT_MIN_EVIDENCE_SAMPLES = 1


RULE_AGENT_MIN_EVIDENCE_SAMPLES = 1



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


def deterministic_rule_review_gate(row: sqlite3.Row, model_review: dict[str, Any]) -> tuple[str, float, str]:
    """Constrain an AI recommendation before it is shown as auto-acceptable."""
    raw_decision = str(model_review.get("decision") or "needs_review").strip().lower()
    confidence = max(0.0, min(1.0, float(model_review.get("confidence") or 0)))
    reason = str(model_review.get("reason") or "智能体未提供充分理由").strip()[:1000]
    if raw_decision in {"reject", "rejected"}:
        return "reject", confidence, reason
    checks: list[str] = []
    operation = str(row["operation"] or "").strip().lower()
    if operation not in {"replace", "normalize", "trim"}:
        checks.append("仅允许 replace/normalize/trim 自动处理")
    if row["risk_level"] != "low":
        checks.append("风险等级不是 low")
    if float(row["confidence"] or 0) < 0.85:
        checks.append("规则草案置信度低于 0.85")
    if int(row["expected_count"] or 0) <= 0:
        checks.append("预计影响数量为 0")
    examples = json.loads(row["examples_json"] or "[]")
    if not isinstance(examples, list) or len(examples) < 2:
        checks.append("有效前后样本少于 2 条")
    if operation == "replace":
        parameters = json.loads(row["parameters_json"] or "{}")
        if not str(parameters.get("from") or parameters.get("source") or "").strip():
            checks.append("replace 缺少明确的 from/source")
    if raw_decision not in {"accept_rule", "accept", "approve", "approved"}:
        checks.append("智能体建议需要复核")
    if confidence < 0.85:
        checks.append("智能体审核置信度低于 0.85")
    if checks:
        return "needs_review", confidence, f"{reason}；" + "；".join(checks)
    return "accept_rule", confidence, reason


def auto_process_accepted_rule_agent_proposal(
    connection: sqlite3.Connection,
    proposal: sqlite3.Row,
) -> sqlite3.Row:
    """Replay first; write a preview only after every active case passes."""
    target_rows = rule_agent_target_rows(connection, proposal)
    if not target_rows:
        return proposal
    replay_id = f"replay-agent-eval-{proposal['proposal_id']}-{hashlib.sha256(proposal['rule_version'].encode()).hexdigest()[:12]}"
    evaluation = replay_rule_agent_against_evaluation_cases(connection, proposal, replay_id)
    now = utc_now()
    status = "replayed" if evaluation["status"] == "passed" else "failed"
    preview_path: Path | None = None
    sample_path: Path | None = None
    preview_sha: str | None = None
    if status == "replayed":
        preview_path, sample_path, preview_sha = rule_agent_write_preview(proposal, target_rows)
    connection.execute(
        """
        UPDATE rule_agent_proposal
        SET status=?,preview_path=?,sample_path=?,preview_sha256=?,preview_count=?,
            replay_count=?,replay_pass_count=?,replay_fail_count=?,
            evaluation_replay_id=?,evaluation_count=?,evaluation_pass_count=?,evaluation_fail_count=?,
            replay_message=?,updated_at=?
        WHERE proposal_id=?
        """,
        (
            status,
            str(preview_path) if preview_path else None,
            str(sample_path) if sample_path else None,
            preview_sha,
            len(target_rows) if status == "replayed" else 0,
            len(target_rows) if status == "replayed" else 0,
            len(target_rows) if status == "replayed" else 0,
            0 if status == "replayed" else len(target_rows),
            evaluation["replayId"],
            evaluation["evaluationCount"],
            evaluation["passCount"],
            evaluation["failCount"],
            None if status == "replayed" else json.dumps(evaluation["failures"][:50], ensure_ascii=False),
            now,
            proposal["proposal_id"],
        ),
    )
    connection.execute(
        "INSERT INTO audit_event(entity_type,entity_id,event_type,actor,payload_json,event_at) VALUES (?,?,?,?,?,?)",
        (
            "rule_agent_proposal",
            proposal["proposal_id"],
            "rule_agent_auto_preview_replay_completed",
            "rule-agent-review",
            json.dumps(
                {
                    "proposalId": proposal["proposal_id"],
                    "candidateMatchCount": len(target_rows),
                    "previewCount": len(target_rows) if status == "replayed" else 0,
                    "evaluationReplayId": evaluation["replayId"],
                    "evaluationCount": evaluation["evaluationCount"],
                    "evaluationPassCount": evaluation["passCount"],
                    "evaluationFailCount": evaluation["failCount"],
                    "status": status,
                    "sourceWrite": False,
                    "formalPublication": False,
                },
                ensure_ascii=False,
            ),
            now,
        ),
    )
    return rule_agent_proposal_row(connection, proposal["proposal_id"])


def rule_agent_proposal_payload(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "proposalId": row["proposal_id"],
        "runId": row["run_id"],
        "ruleKey": row["rule_key"],
        "ruleVersion": row["rule_version"],
        "title": row["title"],
        "objective": row["objective"],
        "operation": row["operation"],
        "condition": json.loads(row["condition_json"] or "{}"),
        "parameters": json.loads(row["parameters_json"] or "{}"),
        "scope": json.loads(row["scope_json"] or "{}"),
        "evidence": json.loads(row["evidence_json"] or "{}"),
        "examples": json.loads(row["examples_json"] or "[]"),
        "expectedCount": int(row["expected_count"]),
        "confidence": float(row["confidence"]),
        "riskLevel": row["risk_level"],
        "status": row["status"],
        "discoveryFilterStatus": row["discovery_filter_status"],
        "discoveryFilterReason": row["discovery_filter_reason"],
        "discoveryFilteredAt": row["discovery_filtered_at"],
        "previewPath": row["preview_path"],
        "samplePath": row["sample_path"],
        "previewCount": int(row["preview_count"]),
        "replayCount": int(row["replay_count"]),
        "replayPassCount": int(row["replay_pass_count"]),
        "replayFailCount": int(row["replay_fail_count"]),
        "evaluationReplayId": row["evaluation_replay_id"],
        "evaluationCount": int(row["evaluation_count"] or 0),
        "evaluationPassCount": int(row["evaluation_pass_count"] or 0),
        "evaluationFailCount": int(row["evaluation_fail_count"] or 0),
        "agentReviewDecision": row["agent_review_decision"],
        "agentReviewConfidence": float(row["agent_review_confidence"] or 0),
        "agentReviewReason": row["agent_review_reason"],
        "agentReviewVersion": row["agent_review_version"],
        "agentReviewedAt": row["agent_reviewed_at"],
        "replayMessage": row["replay_message"],
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }


def rule_agent_target_rows(connection: sqlite3.Connection, proposal: sqlite3.Row | dict[str, Any]) -> list[dict[str, Any]]:
    batch = connection.execute("SELECT batch_id FROM batch_run WHERE batch_id=(SELECT batch_id FROM rule_agent_run WHERE run_id=?)", (proposal["run_id"],)).fetchone()
    if batch is None:
        raise HTTPException(status_code=409, detail="规则草案关联的批次不存在")
    rows = connection.execute(
        f"""
        SELECT c.candidate_id,c.original_description,c.candidate_description,
          d.site_id,d.asset_number,d.location_code,d.location_description,
          d.location_parent,d.classification_description
        FROM semantic_candidate c JOIN device_identity d ON d.device_id=c.device_id
        WHERE c.batch_id=? AND c.confidence='high' AND {RULE_AGENT_EVIDENCE_SQL}
          AND c.validator_status='candidate'
          AND c.review_state='pending' AND c.publication_state='unpublished'
          AND length(trim(c.original_description)) > 0
        ORDER BY d.site_id,COALESCE(d.classification_description,''),c.candidate_id
        """,
        (batch["batch_id"],),
    ).fetchall()
    condition = json.loads(proposal["condition_json"] or "{}")
    parameters = json.loads(proposal["parameters_json"] or "{}")
    scope = json.loads(proposal["scope_json"] or "{}")
    target_rows: list[dict[str, Any]] = []
    for row in rows:
        if not rule_agent_scope_matches(row, scope):
            continue
        matched, transformed, reason = rule_agent_transform(row["original_description"] or "", proposal["operation"], condition, parameters)
        if not matched:
            continue
        target_rows.append({
            "CANDIDATE_ID": row["candidate_id"],
            "SITEID": row["site_id"] or "",
            "ASSETNUM": row["asset_number"] or "",
            "ORIGINAL_DESCRIPTION": row["original_description"] or "",
            "PROPOSED_DESCRIPTION": transformed,
            "LOCATION": row["location_code"] or "",
            "LOCATION_DESCRIPTION": row["location_description"] or "",
            "LOCATION_PARENT": row["location_parent"] or "",
            "CLASSIFICATION": row["classification_description"] or "",
            "RULE_KEY": proposal["rule_key"],
            "OPERATION": proposal["operation"],
            "MATCH_REASON": reason,
        })
    return target_rows


def measure_rule_agent_proposal(
    connection: sqlite3.Connection,
    run_id: str,
    rule_key: str,
    operation: str,
    condition: dict[str, Any],
    parameters: dict[str, Any],
    scope: dict[str, Any],
) -> dict[str, Any]:
    """Measure a model proposal against local candidates before persisting it."""
    proposal_ref = {
        "run_id": run_id,
        "rule_key": rule_key,
        "operation": operation,
        "condition_json": json.dumps(condition, ensure_ascii=False),
        "parameters_json": json.dumps(parameters, ensure_ascii=False),
        "scope_json": json.dumps(scope, ensure_ascii=False),
    }
    try:
        target_rows = rule_agent_target_rows(connection, proposal_ref)
    except HTTPException as exc:
        return {
            "matchedCount": 0,
            "sampleCount": 0,
            "examples": [],
            "eligible": False,
            "filterReason": f"local_executor_error:{str(exc.detail)[:200]}",
        }

    examples = [
        {
            "before": row["ORIGINAL_DESCRIPTION"],
            "after": row["PROPOSED_DESCRIPTION"],
            "candidateId": row["CANDIDATE_ID"],
            "siteId": row["SITEID"],
        }
        for row in target_rows[:5]
    ]
    matched_count = len(target_rows)
    sample_count = len(examples)
    eligible = matched_count >= RULE_AGENT_MIN_MATCH_COUNT and sample_count >= RULE_AGENT_MIN_EVIDENCE_SAMPLES
    return {
        "matchedCount": matched_count,
        "sampleCount": sample_count,
        "examples": examples,
        "eligible": eligible,
        "filterReason": "local_match_and_sample_evidence" if eligible else "no_local_match_or_sample_evidence",
    }


def enrich_rule_agent_proposal_from_local_preview(
    connection: sqlite3.Connection,
    proposal: sqlite3.Row,
) -> sqlite3.Row:
    """Replace model-estimated impact with measured local preview evidence."""
    try:
        target_rows = rule_agent_target_rows(connection, proposal)
    except HTTPException:
        return proposal
    examples = json.loads(proposal["examples_json"] or "[]")
    if not isinstance(examples, list) or len(examples) < 2:
        examples = [
            {
                "before": item["ORIGINAL_DESCRIPTION"],
                "after": item["PROPOSED_DESCRIPTION"],
                "candidateId": item["CANDIDATE_ID"],
            }
            for item in target_rows[:5]
        ]
    evidence = json.loads(proposal["evidence_json"] or "{}")
    if not isinstance(evidence, dict):
        evidence = {}
    evidence.update({"matchedCount": len(target_rows), "sampleCount": min(200, len(target_rows)), "measuredFromLocalPreview": True})
    connection.execute(
        """
        UPDATE rule_agent_proposal
        SET expected_count=?,examples_json=?,evidence_json=?,updated_at=?
        WHERE proposal_id=?
        """,
        (len(target_rows), json.dumps(examples, ensure_ascii=False), json.dumps(evidence, ensure_ascii=False), utc_now(), proposal["proposal_id"]),
    )
    return rule_agent_proposal_row(connection, proposal["proposal_id"])


def rule_agent_write_preview(proposal: sqlite3.Row, rows: list[dict[str, Any]]) -> tuple[Path, Path, str]:
    directory = RULE_AGENT_DIR / proposal["proposal_id"]
    directory.mkdir(parents=True, exist_ok=True)
    preview_path = directory / "preview.csv"
    sample_path = directory / "sample_200.csv"
    fieldnames = ["CANDIDATE_ID", "SITEID", "ASSETNUM", "ORIGINAL_DESCRIPTION", "PROPOSED_DESCRIPTION", "LOCATION", "LOCATION_DESCRIPTION", "LOCATION_PARENT", "CLASSIFICATION", "RULE_KEY", "OPERATION", "MATCH_REASON"]
    sample_rows = rule_agent_stratified_sample(rows, sample_size=200)
    for path, values in ((preview_path, rows), (sample_path, sample_rows)):
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(values)
    return preview_path, sample_path, sha256_file(preview_path)


def rule_agent_case_scope_matches(case: sqlite3.Row, scope: dict[str, Any]) -> bool:
    """Match an evaluation case using only persisted case context."""
    context: dict[str, Any] = {}
    try:
        parsed = json.loads(case["context_json"] or "{}")
        if isinstance(parsed, dict):
            context = parsed
    except json.JSONDecodeError:
        context = {}
    row = {
        "site_id": case["site_id"] or "",
        "classification_description": context.get("classificationDescription")
        or context.get("classification")
        or context.get("classification_description")
        or "",
        "location_parent": context.get("locationParent") or context.get("location_parent") or "",
        "location_code": context.get("locationCode") or context.get("location_code") or context.get("kks") or "",
    }
    return rule_agent_scope_matches(row, scope)


def evaluate_rule_agent_catalog_spec(
    connection: sqlite3.Connection,
    spec: dict[str, Any],
) -> dict[str, Any]:
    """Reject a mined pattern before AI review if it regresses active cases."""
    cases = connection.execute("SELECT * FROM evaluation_case WHERE active=1 ORDER BY case_id").fetchall()
    failures: list[dict[str, Any]] = []
    pass_count = 0
    for case in cases:
        if not rule_agent_case_scope_matches(case, spec.get("scope") or {}):
            pass_count += 1
            continue
        matched, transformed, reason = rule_agent_transform(
            case["input_description"],
            spec["operation"],
            spec.get("condition") or {},
            spec.get("parameters") or {},
        )
        message: dict[str, Any] | None = None
        if matched and spec["operation"] in {"keep_original", "block"}:
            message = {"reason": "non_transforming_operation_cannot_change_evaluation_case"}
        elif matched and (
            case["expected_decision"] not in {"approved", "modified"}
            or transformed != case["expected_description"]
        ):
            message = {
                "failureType": case["failure_type"],
                "reason": reason,
                "expectedDecision": case["expected_decision"],
                "expectedDescription": case["expected_description"],
                "actualDescription": transformed,
            }
        if message:
            if len(failures) < 20:
                failures.append({"caseId": case["case_id"], "reason": message})
        else:
            pass_count += 1
    return {
        "evaluationCount": len(cases),
        "evaluationPassCount": pass_count,
        "evaluationFailCount": len(failures) if len(failures) < 20 else len(cases) - pass_count,
        "status": "passed" if not failures else "failed",
        "failures": failures,
    }


def replay_rule_agent_against_evaluation_cases(
    connection: sqlite3.Connection,
    proposal: sqlite3.Row,
    replay_id: str,
) -> dict[str, Any]:
    """Replay a proposed rule against every active historical evaluation case."""
    existing = connection.execute(
        "SELECT replay_id,status,evaluation_count,pass_count,fail_count FROM replay_run WHERE replay_id=?",
        (replay_id,),
    ).fetchone()
    if existing is not None:
        return {
            "replayId": existing["replay_id"],
            "status": existing["status"],
            "evaluationCount": int(existing["evaluation_count"]),
            "passCount": int(existing["pass_count"]),
            "failCount": int(existing["fail_count"]),
            "failures": [],
        }

    cases = connection.execute(
        "SELECT * FROM evaluation_case WHERE active=1 ORDER BY case_id"
    ).fetchall()
    operation = str(proposal["operation"] or "").strip().lower()
    condition = json.loads(proposal["condition_json"] or "{}")
    parameters = json.loads(proposal["parameters_json"] or "{}")
    scope = json.loads(proposal["scope_json"] or "{}")
    started_at = utc_now()
    pass_count = 0
    fail_count = 0
    failures: list[dict[str, str]] = []
    results: list[tuple[str, str, str | None, str, str | None]] = []

    for case in cases:
        expected_decision = case["expected_decision"]
        expected_description = case["expected_description"]
        matched = False
        actual_decision = expected_decision
        actual_description = expected_description
        message: str | None = None
        if rule_agent_case_scope_matches(case, scope):
            matched, transformed, reason = rule_agent_transform(
                case["input_description"], operation, condition, parameters
            )
            if matched and operation not in {"keep_original", "block"}:
                actual_decision = "modified" if transformed != case["input_description"] else "approved"
                actual_description = transformed
                if expected_decision not in {"approved", "modified"} or actual_description != expected_description:
                    message = json.dumps(
                        {
                            "failureType": case["failure_type"],
                            "reason": reason,
                            "expectedDecision": expected_decision,
                            "actualDecision": actual_decision,
                            "expectedDescription": expected_description,
                            "actualDescription": actual_description,
                        },
                        ensure_ascii=False,
                    )
            elif matched and operation in {"keep_original", "block"}:
                message = json.dumps(
                    {"reason": "non_transforming_operation_cannot_change_evaluation_case", "operation": operation},
                    ensure_ascii=False,
                )
        outcome = "fail" if message else "pass"
        if outcome == "pass":
            pass_count += 1
        else:
            fail_count += 1
            if len(failures) < 200:
                failures.append({"caseId": case["case_id"], "reason": message or "evaluation_regression"})
        results.append((case["case_id"], actual_decision, actual_description, outcome, message))

    finished_at = utc_now()
    connection.execute(
        """
        INSERT INTO replay_run
          (replay_id,rule_version,validator_version,evaluation_count,pass_count,fail_count,status,started_at,finished_at)
        VALUES (?,?,?,?,?,?,?,?,?)
        """,
        (
            replay_id,
            proposal["rule_version"],
            "hd-semantic-validator-0.3.0",
            len(cases),
            pass_count,
            fail_count,
            "passed" if fail_count == 0 else "failed",
            started_at,
            finished_at,
        ),
    )
    connection.executemany(
        "INSERT INTO replay_result(replay_id,case_id,actual_decision,actual_description,outcome,message) VALUES (?,?,?,?,?,?)",
        [(replay_id, *result) for result in results],
    )
    connection.execute(
        """
        INSERT INTO audit_event(entity_type,entity_id,event_type,actor,payload_json,event_at)
        VALUES (?,?,?,?,?,?)
        """,
        (
            "rule_agent_proposal",
            proposal["proposal_id"],
            "rule_agent_evaluation_replay_completed",
            "rule-agent-gate",
            json.dumps(
                {
                    "proposalId": proposal["proposal_id"],
                    "replayId": replay_id,
                    "evaluationCount": len(cases),
                    "passCount": pass_count,
                    "failCount": fail_count,
                    "sourceWrite": False,
                    "formalPublication": False,
                },
                ensure_ascii=False,
            ),
            finished_at,
        ),
    )
    return {
        "replayId": replay_id,
        "status": "passed" if fail_count == 0 else "failed",
        "evaluationCount": len(cases),
        "passCount": pass_count,
        "failCount": fail_count,
        "failures": failures,
    }


def rule_agent_proposal_row(connection: sqlite3.Connection, proposal_id: str) -> sqlite3.Row:
    row = connection.execute("SELECT * FROM rule_agent_proposal WHERE proposal_id=?", (proposal_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"规则草案不存在：{proposal_id}")
    if row["discovery_filter_status"] != "eligible":
        raise HTTPException(status_code=409, detail="规则草案已被本地证据门禁隔离，不能继续预览、回放或启用")
    return row


