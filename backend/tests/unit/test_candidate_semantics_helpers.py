from __future__ import annotations

import sqlite3

from app.domains.candidates.cluster_rules import (
    ai_cluster_id,
    classify_ai_cluster,
    cluster_pattern,
    summarize_ai_clusters,
)
from app.domains.candidates.serializers import row_to_candidate


def test_candidate_projection_is_stable_and_does_not_expose_extra_columns() -> None:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    row = connection.execute(
        """
        SELECT 'c1' candidate_id,'b1' batch_id,'S1' site_id,'A1' asset_number,
          '原文' original_description,'候选' candidate_description,'K1' location_code,
          '锅炉房' location_description,'一期' location_parent,'泵' classification_description,
          'high' confidence,'candidate' validator_status,'pending' review_state,
          '[\"R1\"]' reason_codes_json,'strong' evidence_level,'created' created_at
        """
    ).fetchone()
    assert row_to_candidate(row) == {
        "candidateId": "c1",
        "batchId": "b1",
        "siteId": "S1",
        "assetNumber": "A1",
        "originalDescription": "原文",
        "candidateDescription": "候选",
        "kks": "K1",
        "locationDescription": "锅炉房",
        "locationParent": "一期",
        "classificationDescription": "泵",
        "confidence": "high",
        "validatorStatus": "candidate",
        "reviewState": "pending",
        "reasonCodes": ["R1"],
        "evidenceLevel": "strong",
        "updatedAt": "created",
    }


def test_cluster_rules_are_deterministic_and_conservative() -> None:
    assert cluster_pattern("leading_minus", "-循环泵12", "循环泵12") == "循环泵{N}"
    assert ai_cluster_id("leading_minus", "循环泵{N}") == ai_cluster_id("leading_minus", "循环泵{N}")
    assert classify_ai_cluster("-循环泵", "循环泵")[2] == "keep_original"
    assert classify_ai_cluster("一期？泵", "一期?泵")[2] == "needs_review"
    assert classify_ai_cluster("一期泵-", "一期泵")[2] == "needs_review"


def test_cluster_summary_counts_members_and_pending_decisions() -> None:
    base = {
        "clusterId": "cluster-1",
        "clusterType": "leading_minus",
        "clusterLabel": "前导负号",
        "clusterPattern": "循环泵{N}",
        "ruleSignature": "leading_minus:循环泵{N}",
        "aiDecision": "keep_original",
        "aiConfidence": 0.99,
        "aiReason": "保留原文",
        "clusterDecision": None,
        "memberDecision": None,
    }
    rows = [
        {**base, "candidateId": "c1", "siteId": "S1", "assetNumber": "A1", "classificationDescription": "泵", "originalDescription": "-循环泵1", "candidateDescription": "循环泵1", "kks": "", "locationDescription": "", "locationParent": ""},
        {**base, "candidateId": "c2", "siteId": "S2", "assetNumber": "A2", "classificationDescription": "泵", "originalDescription": "-循环泵2", "candidateDescription": "循环泵2", "kks": "", "locationDescription": "", "locationParent": "", "memberDecision": "keep_original"},
    ]
    clusters, summary = summarize_ai_clusters(rows)
    assert len(clusters) == 1
    assert clusters[0]["memberCount"] == 2
    assert clusters[0]["reviewedCount"] == 1
    assert summary["pendingClusterCount"] == 1
    assert summary["aiRecommendation"]["keep_original"] == 1
