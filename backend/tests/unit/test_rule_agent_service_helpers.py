"""Unit tests for pure rule-agent service helpers.

These tests cover the deterministic canonicalization, review gates and payload
projections without opening project databases.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
from pathlib import Path

from app.domains.rule_agent import replay as rule_agent_replay
from app.domains.rule_agent.service import (
    canonicalize_rule_agent_proposals,
    canonicalize_semantic_reasoning,
    context_rule_spec_from_agent,
    deterministic_rule_review_gate,
    evaluate_rule_agent_catalog_spec,
    replay_rule_agent_against_evaluation_cases,
    rule_agent_case_scope_matches,
    rule_agent_proposal_payload,
    rule_agent_write_preview,
    semantic_description_skeleton,
    semantic_reasoning_item_payload,
)


def _row(mapping: dict[str, object]) -> sqlite3.Row:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    keys = list(mapping)
    columns = ",".join(f"? AS {key}" for key in keys)
    row = connection.execute(
        f"SELECT {columns}",
        list(mapping.values()),
    ).fetchone()
    connection.close()
    return row


def test_semantic_description_skeleton_normalizes_digits_and_whitespace() -> None:
    assert semantic_description_skeleton("１２３  泵") == "<NUM> 泵"
    assert semantic_description_skeleton("　泵 ") == "泵"


def test_canonicalize_semantic_reasoning_maps_aliases_and_invalid_dsl() -> None:
    clusters = [
        {"clusterKey": "c1", "patternKey": "context.cluster.c1"},
        {"clusterKey": "c2", "patternKey": "context.cluster.c2"},
    ]
    items = [
        {
            "clusterKey": "context.cluster.c1",
            "decision": "propose",
            "confidence": "0.95",
            "riskLevel": "high",
            "candidateRule": {
                "operation": "replace",
                "condition": {"contains": "泵"},
                "parameters": {"from": "A", "to": "B"},
                "scope": {"sites": ["S1"]},
            },
            "evidence": {"reason": "ok"},
            "counterexamples": [],
        },
        {
            "clusterKey": "c2",
            "decision": "keep",
            "confidence": 0.5,
            "candidateRule": {"operation": "replace", "parameters": {"from": "X", "to": "X"}},
        },
    ]
    result = canonicalize_semantic_reasoning(items, clusters)
    assert result[0]["decision"] == "propose_rule"
    assert result[0]["candidateRule"]["parameters"] == {"from": "A", "to": "B"}
    assert result[0]["riskLevel"] == "high"
    assert result[1]["decision"] == "keep_original"
    # Invalid replace from==to should fall back to needs_review with a check.
    bad = canonicalize_semantic_reasoning(
        [{"clusterKey": "c1", "decision": "propose_rule", "candidateRule": {"operation": "replace", "parameters": {"from": "X", "to": "X"}}}],
        clusters,
    )
    assert bad[0]["decision"] == "needs_review"
    assert "candidate_rule_dsl_invalid" in bad[0]["requiredChecks"]


def test_context_rule_spec_from_agent_validates_dsl() -> None:
    valid = context_rule_spec_from_agent(
        {
            "operation": "replace",
            "condition": {"contains": "？"},
            "parameters": {"from": "？", "to": " "},
            "scope": {"sites": ["S1"]},
            "ruleKey": "context.term",
        },
        "context.term.abc",
    )
    assert valid is not None
    assert valid["operation"] == "replace"
    assert valid["parameters"] == {"from": "？", "to": " "}
    assert valid["scope"] == {"sites": ["S1"]}
    assert context_rule_spec_from_agent({"operation": "normalize"}, "bad key") is None
    assert context_rule_spec_from_agent({"operation": "replace", "parameters": {"from": "A", "to": "A"}}, "context.term.abc") is None


def test_canonicalize_rule_agent_proposals_binds_catalog_and_rejects_unknown() -> None:
    catalog = [
        {
            "patternKey": "local.normalize.whitespace",
            "ruleKey": "format.normalize.whitespace",
            "title": "Whitespace",
            "objective": "Normalize whitespace",
            "operation": "normalize",
            "condition": {},
            "parameters": {"mode": "whitespace"},
            "scope": {},
            "riskLevel": "low",
            "confidence": 0.98,
            "matchedCount": 3,
            "sampleCount": 3,
            "examples": [],
            "evidenceSource": "local",
            "catalogVersion": "v1",
        }
    ]
    selected, rejected, fallback = canonicalize_rule_agent_proposals(
        [
            {"patternKey": "local.normalize.whitespace", "confidence": 0.9, "riskLevel": "low", "evidence": {"reason": "ok"}},
            {"patternKey": "local.normalize.whitespace", "confidence": 0.9},
            {"patternKey": "missing.key", "operation": "replace"},
        ],
        catalog,
    )
    assert len(selected) == 1
    assert selected[0]["ruleKey"] == "format.normalize.whitespace"
    assert rejected[0]["reason"] == "duplicate_pattern_key"
    assert rejected[1]["reason"] == "unsupported_or_missing_pattern_key"
    assert fallback is False


def test_deterministic_rule_review_gate_enforces_safety_checks() -> None:
    good = _row(
        {
            "operation": "replace",
            "risk_level": "low",
            "confidence": 0.9,
            "expected_count": 3,
            "examples_json": json.dumps([{"a": 1}, {"a": 2}]),
            "parameters_json": json.dumps({"from": "A", "to": "B"}),
        }
    )
    assert deterministic_rule_review_gate(good, {"decision": "accept_rule", "confidence": 0.9, "reason": "ok"})[0] == "accept_rule"
    bad = _row(
        {
            "operation": "replace",
            "risk_level": "high",
            "confidence": 0.5,
            "expected_count": 0,
            "examples_json": "[]",
            "parameters_json": "{}",
        }
    )
    decision, _, reason = deterministic_rule_review_gate(bad, {"decision": "accept", "confidence": 0.5, "reason": "low"})
    assert decision == "needs_review"
    assert "风险等级不是 low" in reason


def test_rule_agent_payload_projections_are_stable() -> None:
    proposal = _row(
        {
            "proposal_id": "p1",
            "run_id": "r1",
            "rule_key": "format.test",
            "rule_version": "v1",
            "title": "t",
            "objective": "o",
            "operation": "replace",
            "condition_json": '{"contains":"A"}',
            "parameters_json": '{"from":"A","to":"B"}',
            "scope_json": "{}",
            "evidence_json": '{"reason":"r"}',
            "examples_json": '["x"]',
            "expected_count": 2,
            "confidence": 0.9,
            "risk_level": "low",
            "status": "draft",
            "discovery_filter_status": "eligible",
            "discovery_filter_reason": None,
            "discovery_filtered_at": None,
            "preview_path": None,
            "sample_path": None,
            "preview_count": 0,
            "replay_count": 0,
            "replay_pass_count": 0,
            "replay_fail_count": 0,
            "evaluation_replay_id": None,
            "evaluation_count": 0,
            "evaluation_pass_count": 0,
            "evaluation_fail_count": 0,
            "agent_review_decision": None,
            "agent_review_confidence": 0,
            "agent_review_reason": None,
            "agent_review_version": None,
            "agent_reviewed_at": None,
            "replay_message": None,
            "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00",
        }
    )
    payload = rule_agent_proposal_payload(proposal)
    assert payload["proposalId"] == "p1"
    assert payload["condition"] == {"contains": "A"}
    assert payload["expectedCount"] == 2

    reasoning = _row(
        {
            "reasoning_item_id": "i1",
            "reasoning_run_id": "run1",
            "cluster_key": "c1",
            "decision": "keep_original",
            "hypothesis": "h",
            "evidence_json": '{"reason":"r"}',
            "counterexamples_json": '["e"]',
            "candidate_rule_json": "{}",
            "confidence": 0.8,
            "risk_level": "low",
            "required_checks_json": '["c"]',
            "created_at": "2026-01-01T00:00:00+00:00",
        }
    )
    assert semantic_reasoning_item_payload(reasoning)["runId"] == "run1"


def test_rule_agent_case_scope_matches_uses_context_json() -> None:
    case = _row(
        {
            "site_id": "S1",
            "context_json": json.dumps({"classificationDescription": "泵", "locationParent": "一期", "locationCode": "KKS-1"}),
        }
    )
    assert rule_agent_case_scope_matches(case, {"sites": ["S1"], "classifications": ["泵"], "kksPrefixes": ["KKS-"]})
    assert not rule_agent_case_scope_matches(case, {"sites": ["S2"]})


def test_evaluate_rule_agent_catalog_spec_counts_pass_and_fail() -> None:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute(
        """
        CREATE TABLE evaluation_case (
          case_id TEXT PRIMARY KEY,
          active INTEGER,
          site_id TEXT,
          classification_description TEXT,
          location_parent TEXT,
          location_code TEXT,
          context_json TEXT,
          input_description TEXT,
          expected_decision TEXT,
          expected_description TEXT,
          failure_type TEXT
        )
        """
    )
    connection.executemany(
        """
        INSERT INTO evaluation_case
          (case_id,active,site_id,classification_description,location_parent,location_code,
           context_json,input_description,expected_decision,expected_description,failure_type)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """,
        [
            ("c1", 1, "S1", "泵", "一期", "KKS-1", json.dumps({"classificationDescription": "泵"}), "A泵", "modified", "B泵", None),
            ("c2", 1, "S2", "阀", "二期", "KKS-2", json.dumps({"classificationDescription": "阀"}), "C阀", "approved", "C阀", None),
        ],
    )
    spec = {
        "operation": "replace",
        "condition": {"contains": "阀"},
        "parameters": {"from": "C", "to": "D"},
        "scope": {},
    }
    result = evaluate_rule_agent_catalog_spec(connection, spec)
    assert result["evaluationCount"] == 2
    assert result["evaluationPassCount"] == 1
    assert result["evaluationFailCount"] == 1
    assert result["status"] == "failed"
    connection.close()


def test_replay_rule_agent_against_evaluation_cases_is_idempotent() -> None:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        CREATE TABLE evaluation_case (
          case_id TEXT PRIMARY KEY,
          active INTEGER,
          site_id TEXT,
          classification_description TEXT,
          location_parent TEXT,
          location_code TEXT,
          context_json TEXT,
          input_description TEXT,
          expected_decision TEXT,
          expected_description TEXT,
          failure_type TEXT
        );
        CREATE TABLE replay_run (
          replay_id TEXT PRIMARY KEY,
          rule_version TEXT,
          validator_version TEXT,
          evaluation_count INTEGER,
          pass_count INTEGER,
          fail_count INTEGER,
          status TEXT,
          started_at TEXT,
          finished_at TEXT
        );
        CREATE TABLE replay_result (
          replay_id TEXT,
          case_id TEXT,
          actual_decision TEXT,
          actual_description TEXT,
          outcome TEXT,
          message TEXT
        );
        CREATE TABLE audit_event (
          entity_type TEXT,
          entity_id TEXT,
          event_type TEXT,
          actor TEXT,
          payload_json TEXT,
          event_at TEXT
        );
        """
    )
    connection.execute(
        """
        INSERT INTO evaluation_case
          (case_id,active,site_id,classification_description,location_parent,location_code,
           context_json,input_description,expected_decision,expected_description,failure_type)
        VALUES ('c1',1,'S1','泵','一期','KKS-1',?,'A泵','modified','B泵',NULL)
        """,
        (json.dumps({"classificationDescription": "泵"}),),
    )
    proposal = _row(
        {
            "proposal_id": "p1",
            "rule_version": "v1",
            "operation": "replace",
            "condition_json": json.dumps({"contains": "泵"}),
            "parameters_json": json.dumps({"from": "A", "to": "B"}),
            "scope_json": json.dumps({}),
        }
    )
    first = replay_rule_agent_against_evaluation_cases(connection, proposal, "replay-1")
    assert first["status"] == "passed"
    assert first["evaluationCount"] == 1
    assert first["passCount"] == 1
    second = replay_rule_agent_against_evaluation_cases(connection, proposal, "replay-1")
    assert second["replayId"] == "replay-1"
    assert second["status"] == "passed"
    connection.close()


def test_rule_agent_write_preview_creates_csv_and_digest(monkeypatch) -> None:
    tmp = Path(tempfile.mkdtemp(prefix="rule-agent-preview-"))
    try:
        monkeypatch.setattr(rule_agent_replay, "RULE_AGENT_DIR", tmp)
        proposal = _row({"proposal_id": "p1", "rule_key": "format.test", "operation": "replace"})
        rows = [
            {
                "CANDIDATE_ID": "c1",
                "SITEID": "S1",
                "ASSETNUM": "A1",
                "ORIGINAL_DESCRIPTION": "A泵",
                "PROPOSED_DESCRIPTION": "B泵",
                "LOCATION": "KKS-1",
                "LOCATION_DESCRIPTION": "一期",
                "LOCATION_PARENT": "一期",
                "CLASSIFICATION": "泵",
                "RULE_KEY": "format.test",
                "OPERATION": "replace",
                "MATCH_REASON": "replace",
            }
        ]
        preview, sample, digest = rule_agent_write_preview(proposal, rows)
        assert preview.exists()
        assert sample.exists()
        assert digest == rule_agent_replay.sha256_file(preview)
        assert "CANDIDATE_ID" in preview.read_text(encoding="utf-8-sig")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
