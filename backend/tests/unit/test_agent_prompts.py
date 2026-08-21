"""单元测试：app.agent.prompts 的载荷构造与截断上限。"""
from __future__ import annotations

import json

import pytest

from app.agent.prompts import rule_agent_payload, semantic_reasoning_payload

pytestmark = pytest.mark.unit


def _profile(**overrides) -> dict:
    profile = {
        "examples": [{"candidateId": f"c{i}"} for i in range(10)],
        "sites": [{"siteId": f"s{i}"} for i in range(30)],
        "classifications": [{"value": f"v{i}"} for i in range(30)],
        "localRuleCatalog": [
            {"patternKey": f"p{i}", "examples": [1, 2, 3, 4, 5]} for i in range(5)
        ],
        "blockedRuleCatalog": [{"patternKey": f"b{i}"} for i in range(15)],
        "semanticClusters": [
            {
                "patternKey": f"ctx{i}",
                "examples": list(range(10)),
                "descriptions": list(range(10)),
            }
            for i in range(40)
        ],
    }
    profile.update(overrides)
    return profile


def test_rule_agent_payload_shape_and_truncation() -> None:
    payload = json.loads(rule_agent_payload(_profile()))
    assert set(payload.keys()) == {"task", "policy", "output_schema", "profile"}
    profile = payload["profile"]
    assert len(profile["examples"]) == 5
    assert len(profile["sites"]) == 20
    assert len(profile["classifications"]) == 20
    assert all(len(item["examples"]) <= 3 for item in profile["localRuleCatalog"])
    assert len(profile["blockedRuleCatalog"]) == 10
    assert len(profile["semanticClusters"]) == 30
    assert all(len(item["examples"]) <= 3 for item in profile["semanticClusters"])
    assert all(len(item["descriptions"]) <= 6 for item in profile["semanticClusters"])


def test_rule_agent_payload_task_mentions_max_proposals() -> None:
    payload = json.loads(rule_agent_payload(_profile()))
    assert "最多" in payload["task"]
    assert "条" in payload["task"]


def test_rule_agent_payload_context_pattern_keys() -> None:
    payload = json.loads(rule_agent_payload(_profile()))
    keys = payload["profile"]["contextPatternKeys"]
    assert keys == [f"ctx{i}" for i in range(30)]


def test_semantic_reasoning_payload_shape_and_truncation() -> None:
    clusters = [
        {
            "clusterKey": f"k{i}",
            "examples": list(range(10)),
            "descriptions": list(range(10)),
        }
        for i in range(3)
    ]
    payload = json.loads(semantic_reasoning_payload(clusters))
    assert set(payload.keys()) == {"task", "policy", "output_schema", "clusters"}
    assert all(len(c["examples"]) <= 4 for c in payload["clusters"])
    assert all(len(c["descriptions"]) <= 8 for c in payload["clusters"])
