"""单元测试：app.agent.client 的错误信封与 JSON 解析。

只覆盖确定性路径（解析、错误封装、未配置分支），不发起真实模型调用。
"""
from __future__ import annotations

import json

import pytest

from app.agent.client import (
    RuleAgentCallError,
    invoke_rule_agent_completion,
    parse_rule_agent_json,
    rule_agent_failure_detail,
    rule_agent_failure_fields,
)

pytestmark = pytest.mark.unit


def test_error_carries_stable_fields() -> None:
    exc = RuleAgentCallError("bad_input", "message", True, 3)
    assert exc.code == "bad_input"
    assert exc.message == "message"
    assert exc.retryable is True
    assert exc.attempts == 3


def test_parse_plain_json_object() -> None:
    assert parse_rule_agent_json('{"a": 1}', "t") == {"a": 1}


def test_parse_markdown_json_fence() -> None:
    text = '```json\n{"a": 1}\n```'
    assert parse_rule_agent_json(text, "t") == {"a": 1}


def test_parse_markdown_fence_without_language() -> None:
    text = '```\n{"a": 1}\n```'
    assert parse_rule_agent_json(text, "t") == {"a": 1}


def test_parse_recovers_json_amid_prose() -> None:
    text = 'Here is the result: {"a": 1} thanks'
    assert parse_rule_agent_json(text, "t") == {"a": 1}


def test_parse_invalid_json_raises() -> None:
    with pytest.raises(RuleAgentCallError) as excinfo:
        parse_rule_agent_json("not json at all", "t")
    assert excinfo.value.code == "invalid_json"
    assert excinfo.value.retryable is True
    assert excinfo.value.attempts == 1


def test_failure_detail_roundtrip() -> None:
    exc = RuleAgentCallError("provider_http_500", "失败", False, 2)
    payload = json.loads(rule_agent_failure_detail(exc))
    assert payload == {
        "code": "provider_http_500",
        "message": "失败",
        "retryable": False,
        "attempts": 2,
    }


def test_failure_fields_from_json_string() -> None:
    assert rule_agent_failure_fields(
        '{"code":"x","retryable":true,"attempts":2}'
    ) == ("x", True, 2)


def test_failure_fields_defaults_for_missing_keys() -> None:
    assert rule_agent_failure_fields('{"code":"x"}') == ("x", False, 0)


def test_failure_fields_unknown_on_bad_input() -> None:
    assert rule_agent_failure_fields("not json") == ("unknown_agent_error", False, 0)
    assert rule_agent_failure_fields('{"code":"x","attempts":"-5"}') == ("x", False, 0)


def test_invoke_unconfigured_raises_agent_not_configured(monkeypatch) -> None:
    monkeypatch.setattr("app.agent.client.RULE_AGENT_API_KEY", "")
    monkeypatch.setattr("app.agent.client.RULE_AGENT_BASE_URL", "")
    monkeypatch.setattr("app.agent.client.RULE_AGENT_MODEL", "")
    with pytest.raises(RuleAgentCallError) as excinfo:
        invoke_rule_agent_completion([{"role": "user", "content": "hi"}], "test-task")
    assert excinfo.value.code == "agent_not_configured"
    assert excinfo.value.retryable is False
    assert excinfo.value.attempts == 0
