"""Validation of the structured Agent Semantic Contract."""
from __future__ import annotations

from typing import Any


def validate_agent_semantic_result(context: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    """Require claims and decisions to cite evidence from the supplied context."""
    evidence_items = context.get("evidence") or []
    evidence_ids: set[str] = set()
    for item in evidence_items:
        if not isinstance(item, dict):
            continue
        for key in ("evidenceId", "sourceRecordId", "source_record_id", "sourceId", "source_id", "sourceUri", "source_uri"):
            value = str(item.get(key) or "").strip()
            if value:
                evidence_ids.add(value)
    failures: list[dict[str, Any]] = []
    claims = result.get("claims") or []
    if not isinstance(claims, list):
        failures.append({"code": "CLAIMS_NOT_ARRAY"})
        claims = []
    for index, claim in enumerate(claims):
        if not isinstance(claim, dict):
            failures.append({"code": "CLAIM_NOT_OBJECT", "index": index})
            continue
        text = str(claim.get("text") or claim.get("claim") or "").strip()
        refs = claim.get("evidenceIds") or claim.get("evidence") or []
        if not text:
            failures.append({"code": "CLAIM_TEXT_MISSING", "index": index})
        if not isinstance(refs, list) or not refs:
            failures.append({"code": "CLAIM_EVIDENCE_MISSING", "index": index})
            continue
        normalized_refs = [
            str(ref.get("evidenceId") or ref.get("sourceRecordId") or ref)
            if isinstance(ref, dict) else str(ref)
            for ref in refs
        ]
        missing = [ref for ref in normalized_refs if ref not in evidence_ids]
        if missing:
            failures.append({"code": "CLAIM_EVIDENCE_NOT_IN_CONTEXT", "index": index, "missing": missing})
    confidence = result.get("confidence")
    try:
        confidence_value = float(confidence) if confidence is not None else 0.0
    except (TypeError, ValueError):
        confidence_value = -1.0
    if not 0.0 <= confidence_value <= 1.0:
        failures.append({"code": "CONFIDENCE_OUT_OF_RANGE"})
    decision = result.get("decision")
    suggested_actions = result.get("suggestedActions", result.get("suggested_actions", []))
    if suggested_actions is None:
        suggested_actions = []
    if not isinstance(suggested_actions, list):
        failures.append({"code": "SUGGESTED_ACTIONS_NOT_ARRAY"})
        suggested_actions = []
    return {
        "schemaVersion": "agent-semantic-result-v1",
        "status": "passed" if not failures else "blocked",
        "claims": claims,
        "decision": decision if isinstance(decision, dict) else {},
        "suggestedActions": suggested_actions,
        "confidence": confidence_value,
        "evidenceAvailable": len(evidence_ids),
        "failures": failures,
        "policy": {"candidateOnly": True, "mayPublishKnowledge": False, "mayExecuteAction": False},
        "sourceWrite": False,
        "formalPublication": False,
    }
