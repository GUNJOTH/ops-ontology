"""Agent output must remain evidence-bound and non-executing."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from app.domains.semantic.agent_contract import validate_agent_semantic_result  # noqa: E402


def test_agent_contract_accepts_claim_with_context_evidence() -> None:
    result = validate_agent_semantic_result(
        {"evidence": [{"sourceRecordId": "SRC-1"}]},
        {"claims": [{"text": "设备存在异常", "evidenceIds": ["SRC-1"]}], "confidence": 0.91, "suggestedActions": []},
    )
    assert result["status"] == "passed"
    assert result["policy"]["mayPublishKnowledge"] is False


def test_agent_contract_blocks_unknown_evidence() -> None:
    result = validate_agent_semantic_result(
        {"evidence": [{"sourceRecordId": "SRC-1"}]},
        {"claims": [{"text": "设备存在异常", "evidenceIds": ["SRC-2"]}], "confidence": 0.91},
    )
    assert result["status"] == "blocked"
    assert any(item["code"] == "CLAIM_EVIDENCE_NOT_IN_CONTEXT" for item in result["failures"])
