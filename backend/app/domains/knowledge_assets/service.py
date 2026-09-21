"""Knowledge asset payload helpers."""
from __future__ import annotations

import json
import pathlib
import re
import sqlite3
from typing import Any

from pipeline.contracts import connect_local
from pipeline.knowledge.documents import import_document
from pipeline.knowledge.lifecycle import approve_knowledge, create_candidate, release_approved_knowledge
from pipeline.knowledge.quality import detect_conflicts, replay_knowledge

from app.agent.client import RuleAgentCallError, invoke_rule_agent_completion, parse_rule_agent_json
from app.core.config import (
    KNOWLEDGE_INPUT_DIR,
    RULE_AGENT_API_KEY,
    RULE_AGENT_BASE_URL,
    RULE_AGENT_MODEL,
    UNIFIED_SEMANTICS_DB,
)


def knowledge_asset_row(row: sqlite3.Row, source_count: int, issue_count: int) -> dict[str, Any]:
    columns = set(row.keys())
    status = row["status"]
    lifecycle = row["lifecycle_status"] if "lifecycle_status" in columns else None
    return {
        "assetId": row["asset_id"],
        "assetKey": row["asset_key"],
        "assetType": row["asset_type"],
        "title": row["title"],
        "canonicalDefinition": row["canonical_definition"],
        "currentVersion": row["current_version"],
        "status": row["status"],
        "lifecycleStatus": lifecycle or status,
        "knowledgeKind": row["knowledge_kind"] if "knowledge_kind" in columns else row["asset_type"],
        "packageId": row["package_id"] if "package_id" in columns else "platform-core",
        "domain": row["knowledge_domain"] if "knowledge_domain" in columns else "enterprise-operations",
        "sourceType": row["source_type"] if "source_type" in columns else None,
        "sourceId": row["source_id"] if "source_id" in columns else None,
        "sourceUri": row["source_uri"] if "source_uri" in columns else None,
        "owner": row["owner"] if "owner" in columns else None,
        "reviewer": row["reviewer"] if "reviewer" in columns else None,
        "confidence": row["confidence"] if "confidence" in columns else None,
        "validFrom": row["valid_from"] if "valid_from" in columns else None,
        "validTo": row["valid_to"] if "valid_to" in columns else None,
        "ontologyVersion": row["ontology_version"] if "ontology_version" in columns else None,
        "qualityScore": row["quality_score"] if "quality_score" in columns else None,
        "sourceScope": row["source_scope"],
        "sourceCount": source_count,
        "issueCount": issue_count,
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }


def knowledge_asset_evidence_payload(connection: sqlite3.Connection, asset_id: str) -> dict[str, Any]:
    """Return source evidence for one asset without exposing source tables."""
    asset = connection.execute("SELECT * FROM knowledge_asset WHERE asset_id=?", (asset_id,)).fetchone()
    if asset is None:
        raise LookupError("知识资产不存在")
    sources = [dict(row) for row in connection.execute(
        "SELECT * FROM knowledge_asset_source WHERE asset_id=? ORDER BY created_at,source_kind,source_record_id",
        (asset_id,),
    ).fetchall()]
    issues = [dict(row) for row in connection.execute(
        "SELECT issue_id,issue_type,severity,status,details_json,created_at,resolved_at FROM knowledge_asset_issue WHERE asset_id=? ORDER BY status,created_at",
        (asset_id,),
    ).fetchall()]
    return {
        "schemaVersion": "knowledge-evidence-v1",
        "assetId": asset_id,
        "sources": sources,
        "issues": issues,
        "hasSnapshotEvidence": any(str(row.get("source_snapshot_id") or "").strip() for row in sources),
        "sourceWrite": False,
        "formalPublication": False,
    }


def knowledge_asset_versions_payload(connection: sqlite3.Connection, asset_id: str) -> dict[str, Any]:
    """Return all immutable versions for one asset."""
    asset = connection.execute("SELECT asset_id,current_version FROM knowledge_asset WHERE asset_id=?", (asset_id,)).fetchone()
    if asset is None:
        raise LookupError("知识资产不存在")
    versions = [dict(row) for row in connection.execute(
        "SELECT * FROM knowledge_asset_version WHERE asset_id=? ORDER BY created_at DESC,version DESC",
        (asset_id,),
    ).fetchall()]
    return {
        "schemaVersion": "knowledge-version-v1",
        "assetId": asset_id,
        "currentVersion": asset["current_version"],
        "versions": versions,
        "sourceWrite": False,
        "formalPublication": False,
    }


def knowledge_for_object_payload(
    connection: sqlite3.Connection,
    object_type: str,
    object_key: str,
    limit: int = 50,
) -> dict[str, Any]:
    """Select only released, evidence-backed knowledge bound to one object."""
    rows = connection.execute(
        """SELECT a.*,b.binding_id,b.relation_type,b.evidence_json AS binding_evidence,
                  b.target_iri,b.package_id AS binding_package_id,b.ontology_version AS binding_ontology_version
             FROM knowledge_asset_binding b
             JOIN knowledge_asset a ON a.asset_id=b.asset_id
            WHERE b.object_type=? AND b.object_key=? AND b.status='accepted'
              AND lower(a.status) IN ('published','enabled')
              AND NOT EXISTS (
                SELECT 1 FROM knowledge_asset_issue i
                 WHERE i.asset_id=a.asset_id AND i.issue_type='conflict'
                   AND i.status='open' AND i.severity IN ('medium','high')
              )
            ORDER BY a.updated_at DESC,a.asset_key LIMIT ?""",
        (object_type, object_key, limit),
    ).fetchall()
    items: list[dict[str, Any]] = []
    for row in rows:
        item = knowledge_asset_row(row, 0, 0)
        item.update({
            "bindingId": row["binding_id"],
            "relationType": row["relation_type"],
            "bindingEvidence": row["binding_evidence"],
            "targetIri": row["target_iri"],
            "bindingPackageId": row["binding_package_id"],
            "bindingOntologyVersion": row["binding_ontology_version"],
        })
        evidence = [dict(source) for source in connection.execute(
            "SELECT source_kind,source_record_id,source_table,source_snapshot_id,source_status,evidence_json,source_uri,page_number,section_path FROM knowledge_asset_source WHERE asset_id=? ORDER BY created_at LIMIT 20",
            (row["asset_id"],),
        ).fetchall()]
        item["evidence"] = evidence
        item["evidenceBacked"] = any(str(source.get("source_snapshot_id") or "").strip() for source in evidence)
        items.append(item)
    return {
        "schemaVersion": "knowledge-object-context-v1",
        "objectType": object_type,
        "objectKey": object_key,
        "items": items,
        "suppressedUnreleasedOrConflicted": True,
        "sourceWrite": False,
        "formalPublication": False,
    }


def _deterministic_candidate(content: str, fragment_id: str) -> dict[str, Any]:
    """Create a conservative candidate when no model is configured."""
    threshold = re.search(r"(?P<subject>[^\n。；]{1,80}?)(?:上限|不得超过|不应超过|≤|<=)\s*(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>℃|°C|kV|MPa|bar|mm|%)?", content)
    if threshold:
        subject = threshold.group("subject").strip(" ，:：")
        value = float(threshold.group("value"))
        return {
            "knowledgeType": "standard",
            "title": f"{subject}限值候选",
            "definition": {"appliesTo": subject, "predicate": "upperLimit", "value": value, "unit": threshold.group("unit") or "", "sourceFragmentId": fragment_id, "confidence": 0.72, "qualityScore": 0.6},
            "extractionMode": "deterministic-fallback",
        }
    return {
        "knowledgeType": "expert",
        "title": "待审核知识候选",
        "definition": {"canonical": content[:2000], "sourceFragmentId": fragment_id, "confidence": 0.2, "qualityScore": 0.2},
        "extractionMode": "deterministic-fallback",
    }


def extract_knowledge_candidate_payload(
    connection: sqlite3.Connection,
    fragment_id: str,
    *,
    use_ai: bool,
    prompt_version: str,
) -> dict[str, Any]:
    """Extract one candidate, preserving model output as a reviewable draft."""
    row = connection.execute("SELECT * FROM knowledge_asset WHERE asset_id=? AND asset_type='fragment'", (fragment_id,)).fetchone()
    if row is None:
        raise LookupError("知识片段不存在")
    content = str(row["canonical_definition"] or "").strip()
    candidate = _deterministic_candidate(content, fragment_id)
    agent_status = "not_requested"
    if use_ai and RULE_AGENT_API_KEY and RULE_AGENT_BASE_URL and RULE_AGENT_MODEL:
        try:
            text = invoke_rule_agent_completion(
                [
                    {"role": "system", "content": "你是企业知识抽取智能体。只能从给定片段抽取候选知识，返回 JSON，不得发布，不得补造来源。"},
                    {"role": "user", "content": json.dumps({"fragmentId": fragment_id, "content": content, "outputSchema": {"knowledgeType": "standard|rule|sop|case|expert", "title": "", "definition": {}, "confidence": 0.0, "evidence": {"fragmentId": fragment_id}}, "promptVersion": prompt_version}, ensure_ascii=False)},
                ],
                "知识候选抽取智能体",
            )
            parsed = parse_rule_agent_json(text, "知识候选抽取智能体")
            if isinstance(parsed, dict) and isinstance(parsed.get("definition"), dict) and str(parsed.get("title") or "").strip():
                candidate = {"knowledgeType": str(parsed.get("knowledgeType") or "expert"), "title": str(parsed["title"])[:500], "definition": {**parsed["definition"], "sourceFragmentId": fragment_id, "confidence": parsed.get("confidence", 0), "qualityScore": parsed.get("qualityScore", parsed.get("confidence", 0))}, "extractionMode": "ai"}
                agent_status = "completed"
        except (RuleAgentCallError, ValueError, TypeError, json.JSONDecodeError):
            agent_status = "fallback"
    result = create_candidate(
        connection, title=candidate["title"], definition=candidate["definition"], source_fragment_id=fragment_id,
        knowledge_kind=str(candidate.get("knowledgeType") or "expert"), extraction_model="configured-agent" if agent_status == "completed" else "deterministic-v1", prompt_version=prompt_version,
    )
    return {**result, "candidate": candidate, "agentStatus": agent_status, "sourceWrite": False, "formalPublication": False}


def import_knowledge_document_payload(request: Any) -> dict[str, Any]:
    path = pathlib.Path(request.source_path).resolve()
    try:
        path.relative_to(KNOWLEDGE_INPUT_DIR.resolve())
    except ValueError as exc:
        raise PermissionError(f"文档必须位于受控知识输入目录：{KNOWLEDGE_INPUT_DIR}") from exc
    connection = connect_local(UNIFIED_SEMANTICS_DB)
    try:
        return import_document(connection, path, package_id=request.package_id, domain=request.domain, source_snapshot_id=request.source_snapshot_id, owner=request.owner)
    finally:
        connection.close()


def extract_knowledge_candidate(request: Any) -> dict[str, Any]:
    connection = connect_local(UNIFIED_SEMANTICS_DB)
    try:
        return extract_knowledge_candidate_payload(connection, request.fragment_id, use_ai=request.use_ai, prompt_version=request.prompt_version)
    finally:
        connection.close()


def build_case_knowledge(request: Any) -> dict[str, Any]:
    """Create a case-knowledge candidate from an imported fragment."""
    connection = connect_local(UNIFIED_SEMANTICS_DB)
    try:
        fragment = connection.execute(
            "SELECT asset_id FROM knowledge_asset WHERE asset_id=? AND asset_type='fragment'",
            (request.source_fragment_id,),
        ).fetchone()
        if fragment is None:
            raise LookupError("案例来源片段不存在")
        definition = dict(request.definition)
        definition.setdefault("sourceFragmentId", request.source_fragment_id)
        definition.setdefault("knowledgeType", "case")
        return create_candidate(
            connection,
            title=request.title,
            definition=definition,
            source_fragment_id=request.source_fragment_id,
            knowledge_kind="case",
            package_id=request.package_id,
            domain=request.domain,
            extraction_model="case-builder-v1",
            prompt_version="case-builder-v1",
        )
    finally:
        connection.close()


def review_knowledge_asset(asset_id: str, request: Any, actor: str) -> dict[str, Any]:
    connection = connect_local(UNIFIED_SEMANTICS_DB)
    try:
        return approve_knowledge(connection, asset_id, decision=request.decision, reviewer=request.reviewer or actor, note=request.note, idempotency_key=request.idempotency_key)
    finally:
        connection.close()


def release_knowledge_assets(request: Any) -> dict[str, Any]:
    connection = connect_local(UNIFIED_SEMANTICS_DB)
    try:
        return release_approved_knowledge(connection, release_id=request.release_id, reviewer=request.reviewer, note=request.note)
    finally:
        connection.close()


def detect_knowledge_conflicts_payload() -> dict[str, Any]:
    connection = connect_local(UNIFIED_SEMANTICS_DB)
    try:
        return detect_conflicts(connection)
    finally:
        connection.close()


def replay_knowledge_payload(asset_id: str, cases: list[dict[str, Any]]) -> dict[str, Any]:
    connection = connect_local(UNIFIED_SEMANTICS_DB)
    try:
        return replay_knowledge(connection, asset_id, cases)
    finally:
        connection.close()


def retrieve_knowledge_payload(
    connection: sqlite3.Connection,
    *,
    query: str,
    object_type: str | None = None,
    object_key: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """Ontology-guided local retrieval: structured + text + accepted bindings."""
    terms = [term.lower() for term in re.findall(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]", query or "")]
    rows = connection.execute(
        """
        SELECT DISTINCT a.* FROM knowledge_asset a
        LEFT JOIN knowledge_asset_source s ON s.asset_id=a.asset_id
        LEFT JOIN knowledge_asset_binding b ON b.asset_id=a.asset_id AND b.status='accepted'
        WHERE lower(a.status) IN ('published','enabled')
          AND s.source_snapshot_id IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM knowledge_asset_issue i WHERE i.asset_id=a.asset_id AND i.issue_type='conflict' AND i.status='open' AND i.severity IN ('medium','high'))
          AND (? IS NULL OR b.object_type=? AND b.object_key=?)
        ORDER BY a.updated_at DESC,a.asset_key
        LIMIT 500
        """,
        (object_type, object_type, object_key),
    ).fetchall()
    ranked: list[tuple[float, sqlite3.Row]] = []
    for row in rows:
        haystack = " ".join(str(row[key] or "") for key in ("title", "canonical_definition", "knowledge_domain", "asset_type")).lower()
        score = sum(haystack.count(term) for term in terms) if terms else 0.0
        if object_type and object_key:
            score += 5.0
        if str(row["status"]).lower() == "published":
            score += 1.0
        if score > 0 or not terms:
            ranked.append((score, row))
    ranked.sort(key=lambda item: (-item[0], item[1]["asset_key"]))
    items = []
    for score, row in ranked[:limit]:
        item = knowledge_asset_row(row, 1, 0)
        item["retrievalScore"] = score
        item["retrievalModes"] = ["structured", "full_text"] + (["graph-binding"] if object_type and object_key else [])
        items.append(item)
    return {
        "schemaVersion": "semantic-retrieval-v1",
        "query": query,
        "objectType": object_type,
        "objectKey": object_key,
        "items": items,
        "total": len(items),
        "strategy": ["ontology-guided", "structured", "full-text", "graph-binding" if object_type and object_key else "no-object-filter"],
        "vector": {"status": "not_enabled", "reason": "首版保持 Canonical RDF + SQLite 权威，不引入未经验证的向量索引"},
        "sourceWrite": False,
        "formalPublication": False,
    }

