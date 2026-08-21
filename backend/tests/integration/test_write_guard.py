"""写守卫覆盖测试：SEMANTIC_API_TOKEN 对所有写端点一致生效（F10）。

口径：
- ``app.core.auth`` 在每次请求时读取环境变量，因此 ``monkeypatch.setenv``
  在测试中立即生效（不再依赖模块导入时快照）。
- 401 断言在守卫层触发，业务逻辑与数据库均不会被执行，无需真实数据。
- token 未设置时守卫是空操作，行为与之前完全一致（无 token 也能写，且
  X-Reviewer 仍被解析为审计 actor）。
"""
from __future__ import annotations

import inspect
import sqlite3

import pytest
from app.core.auth import require_decision_auth
from app.main import (
    agent_audit_pending_candidates,
    ai_auto_approve,
    register_cleaning_rule,
    semantic_execution_dispatch,
)
from fastapi.params import Depends as DependsParam

pytestmark = pytest.mark.integration

TOKEN = "test-write-guard-token"

GUARDED_HANDLERS = {
    "ai_auto_approve": ai_auto_approve,
    "agent_audit_pending_candidates": agent_audit_pending_candidates,
    "semantic_execution_dispatch": semantic_execution_dispatch,
    "register_cleaning_rule": register_cleaning_rule,
}

# 每个端点一个最小合法请求体：401 断言发生在业务逻辑之前，但请求体仍需通过
# pydantic 校验，否则会先返回 422 而非 401。
POST_BODIES = {
    "/api/ai-review/auto-approve": {"idempotencyKey": "guard-test-auto-approve"},
    "/api/ai-review/agent-audit": {"idempotencyKey": "guard-test-agent-audit"},
    "/api/semantic-execution/dispatch": {"idempotencyKey": "guard-test-dispatch"},
    "/api/cleaning/rules": {
        "ruleKey": "guard-rule",
        "cleaningType": "trim",
        "ruleLabel": "守卫测试规则",
        "actionLabel": "清洗",
        "isCleaning": True,
        "replayId": "guard-replay",
        "ruleVersion": "v1",
    },
}


@pytest.mark.parametrize("name,handler", sorted(GUARDED_HANDLERS.items()))
def test_guarded_handler_signature_has_decision_auth(name, handler) -> None:
    """四个写端点必须通过签名挂载 require_decision_auth 依赖。"""
    params = inspect.signature(handler).parameters
    default = params["actor"].default
    assert isinstance(default, DependsParam)
    assert default.dependency is require_decision_auth


@pytest.mark.parametrize("path", sorted(POST_BODIES))
def test_write_endpoint_rejects_missing_token(client, monkeypatch, path) -> None:
    monkeypatch.setenv("SEMANTIC_API_TOKEN", TOKEN)
    response = client.post(path, json=POST_BODIES[path])
    assert response.status_code == 401, f"{path} 未携带 token 应被拦截"


@pytest.mark.parametrize("path", sorted(POST_BODIES))
def test_write_endpoint_rejects_wrong_token(client, monkeypatch, path) -> None:
    monkeypatch.setenv("SEMANTIC_API_TOKEN", TOKEN)
    response = client.post(
        path,
        json=POST_BODIES[path],
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert response.status_code == 401, f"{path} 携带错误 token 应被拦截"


def test_auto_approve_passes_guard_with_token(client, monkeypatch, isolated_workflow_db) -> None:
    """token 正确时守卫放行并进入业务逻辑：空批次返回 503，而非 401。"""
    monkeypatch.setenv("SEMANTIC_API_TOKEN", TOKEN)
    response = client.post(
        "/api/ai-review/auto-approve",
        json=POST_BODIES["/api/ai-review/auto-approve"],
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert response.status_code == 503


def test_agent_audit_passes_guard_with_token(client, monkeypatch, isolated_workflow_db) -> None:
    """token 正确时守卫放行并进入业务逻辑：空批次返回 503，而非 401。"""
    monkeypatch.setenv("SEMANTIC_API_TOKEN", TOKEN)
    response = client.post(
        "/api/ai-review/agent-audit",
        json=POST_BODIES["/api/ai-review/agent-audit"],
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert response.status_code == 503


def test_register_cleaning_rule_with_token_records_operator(
    client, monkeypatch, isolated_workflow_db
) -> None:
    """正确 token 放行，且 X-Reviewer 被解析并记录为审计 actor。"""
    monkeypatch.setenv("SEMANTIC_API_TOKEN", TOKEN)
    response = client.post(
        "/api/cleaning/rules",
        json=POST_BODIES["/api/cleaning/rules"],
        headers={"Authorization": f"Bearer {TOKEN}", "X-Reviewer": "guard-operator"},
    )
    assert response.status_code == 200, response.text

    connection = sqlite3.connect(str(isolated_workflow_db))
    try:
        connection.row_factory = sqlite3.Row
        audit = connection.execute(
            "SELECT actor FROM audit_event "
            "WHERE event_type='cleaning_rule_registered' AND entity_id=?",
            ("guard-rule",),
        ).fetchone()
        assert audit is not None
        assert audit["actor"] == "guard-operator"
    finally:
        connection.close()


def test_register_cleaning_rule_without_token_still_works(
    client, isolated_workflow_db
) -> None:
    """token 未设置时守卫是空操作：注册照常成功，X-Reviewer 仍被记录。"""
    body = dict(POST_BODIES["/api/cleaning/rules"])
    body["ruleKey"] = "guard-rule-notoken"
    body["replayId"] = "guard-replay-notoken"
    response = client.post(
        "/api/cleaning/rules",
        json=body,
        headers={"X-Reviewer": "no-token-reviewer"},
    )
    assert response.status_code == 200, response.text

    connection = sqlite3.connect(str(isolated_workflow_db))
    try:
        connection.row_factory = sqlite3.Row
        audit = connection.execute(
            "SELECT actor FROM audit_event "
            "WHERE event_type='cleaning_rule_registered' AND entity_id=?",
            ("guard-rule-notoken",),
        ).fetchone()
        assert audit is not None
        assert audit["actor"] == "no-token-reviewer"
    finally:
        connection.close()
