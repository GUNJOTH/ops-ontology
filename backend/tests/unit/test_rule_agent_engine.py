from __future__ import annotations

import pytest
from app.domains.rule_agent.engine import (
    rule_agent_scope_matches,
    rule_agent_stratified_sample,
    rule_agent_transform,
)
from fastapi import HTTPException


def test_rule_agent_transform_is_deterministic_and_blocks_unsupported_patterns() -> None:
    assert rule_agent_transform("  泵  ", "trim", {}, {}) == (True, "泵", "trim")
    assert rule_agent_transform("一期泵", "replace", {"contains": "泵"}, {"from": "一期", "to": "二期"}) == (True, "二期泵", "replace")
    assert rule_agent_transform("全角　空格", "normalize", {}, {"mode": "whitespace"}) == (True, "全角 空格", "normalize")
    assert rule_agent_transform("文本", "replace", {"descriptionPattern": "*"}, {"from": "文本", "to": "新文本"})[2] == "unsupported_description_pattern"
    with pytest.raises(HTTPException):
        rule_agent_transform("文本", "replace", {}, {})


def test_rule_agent_scope_accepts_scalar_aliases_and_kks_prefix() -> None:
    row = {"site_id": "S1", "classification_description": "泵", "location_parent": "一期", "location_code": "KKS-001"}
    assert rule_agent_scope_matches(row, {"siteIds": "S1", "classifications": "泵", "kksPrefix": "KKS-"})
    assert not rule_agent_scope_matches(row, {"siteIds": "S2"})


def test_stratified_sample_is_bounded_and_repeatable() -> None:
    rows = [{"SITEID": "S1" if index < 8 else "S2", "value": index} for index in range(10)]
    first = rule_agent_stratified_sample(rows, sample_size=4)
    second = rule_agent_stratified_sample(rows, sample_size=4)
    assert first == second
    assert len(first) == 4
    assert {row["SITEID"] for row in first} == {"S1", "S2"}
