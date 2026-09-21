"""Unit tests for candidate AI-review helper functions.

These tests cover deterministic SQL gate builders and in-memory sample summary
logic without requiring project databases or model calls.
"""
from __future__ import annotations

import sqlite3

from app.domains.candidates.ai_review import ai_sample_summary, auto_approval_filter, candidate_agent_where


def test_candidate_agent_where_builds_safe_gate() -> None:
    where, parameters = candidate_agent_where("batch-1")
    assert "c.batch_id=?" in where
    assert "c.validator_status='candidate'" in where
    assert "c.confidence='high'" in where
    assert parameters == ["batch-1"]

    where_ids, parameters_ids = candidate_agent_where("batch-1", candidate_ids=["c1", "c2"])
    assert "c.candidate_id IN (?,?)" in where_ids
    assert parameters_ids == ["batch-1", "c1", "c2"]

    where_included, _ = candidate_agent_where("batch-1", include_previously_audited=True)
    assert "NOT EXISTS" not in where_included


def test_auto_approval_filter_requires_equivalence_and_optional_sample() -> None:
    where, parameters = auto_approval_filter("batch-1")
    assert "trim(c.original_description) = trim(c.candidate_description)" in where
    assert "c.evidence_level = 'strong'" in where
    assert parameters == ["batch-1"]

    where_sample, parameters_sample = auto_approval_filter("batch-1", sample_id="sample-1")
    assert "si.sample_id=?" in where_sample
    assert parameters_sample == ["batch-1", "sample-1"]


def test_ai_sample_summary_counts_recommendations_and_reviewed() -> None:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute(
        """
        CREATE TABLE ai_review_decision (
          decision TEXT,
          candidate_id TEXT,
          sample_id TEXT
        )
        """
    )
    connection.executemany(
        "INSERT INTO ai_review_decision(decision,candidate_id,sample_id) VALUES (?,?,?)",
        [
            ("keep_original", "c1", "sample-1"),
            ("needs_review", "c2", "sample-1"),
        ],
    )
    rows = [
        {"CANDIDATE_ID": "c1", "AI_DECISION": "保留原文", "SITEID": "S1", "CLASSIFICATION_DESCRIPTION": "泵"},
        {"CANDIDATE_ID": "c2", "AI_DECISION": "需要复核", "SITEID": "S2", "CLASSIFICATION_DESCRIPTION": "阀"},
        {"CANDIDATE_ID": "c3", "AI_DECISION": "接受候选", "SITEID": "S1", "CLASSIFICATION_DESCRIPTION": "泵"},
    ]
    summary = ai_sample_summary(connection, "sample-1", rows)
    assert summary["total"] == 3
    assert summary["reviewed"] == 2
    assert summary["pending"] == 1
    assert summary["aiRecommendation"]["keep_original"] == 1
    assert summary["aiRecommendation"]["accept_candidate"] == 1
    assert summary["pendingByRecommendation"]["accept_candidate"] == 1
    assert summary["sites"][0]["siteId"] == "S1"
    assert summary["sites"][0]["count"] == 2
    connection.close()
