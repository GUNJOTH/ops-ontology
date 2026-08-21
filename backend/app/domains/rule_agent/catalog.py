"""Local evidence catalog and semantic-context profiling for rule discovery.
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
import unicodedata
from typing import Any

from app.core.config import RULE_AGENT_CATALOG_VERSION, RULE_AGENT_MAX_PROPOSALS
from app.domains.candidates.service import latest_batch

from .constants import RULE_AGENT_CONTEXT_SAMPLE_LIMIT, RULE_AGENT_EVIDENCE_SQL
from .engine import rule_agent_transform
from .replay import evaluate_rule_agent_catalog_spec

_SEMANTIC_CONTEXT_CLUSTER_CACHE: dict[tuple[str, int, int], list[dict[str, Any]]] = {}


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
