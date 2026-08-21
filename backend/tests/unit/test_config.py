"""单元测试：app.core.config.load_local_env 的白名单加载规则。"""
from __future__ import annotations

import os

import pytest
from app.core.config import load_local_env

pytestmark = pytest.mark.unit


def test_load_local_env_sets_allowlisted(tmp_dir, monkeypatch) -> None:
    monkeypatch.delenv("RULE_AGENT_API_KEY", raising=False)
    monkeypatch.delenv("RULE_AGENT_MODEL", raising=False)
    env_file = tmp_dir / ".env"
    env_file.write_text(
        'RULE_AGENT_API_KEY="sk-test"\nRULE_AGENT_MODEL=my-model\n', encoding="utf-8"
    )
    load_local_env(env_file)
    assert os.environ["RULE_AGENT_API_KEY"] == "sk-test"
    assert os.environ["RULE_AGENT_MODEL"] == "my-model"


def test_load_local_env_ignores_disallowed_keys(tmp_dir, monkeypatch) -> None:
    monkeypatch.delenv("AWS_SECRET", raising=False)
    env_file = tmp_dir / ".env"
    env_file.write_text("AWS_SECRET=x\n", encoding="utf-8")
    load_local_env(env_file)
    assert "AWS_SECRET" not in os.environ


def test_load_local_env_skips_comments_and_blanks(tmp_dir, monkeypatch) -> None:
    monkeypatch.delenv("RULE_AGENT_TIMEOUT", raising=False)
    env_file = tmp_dir / ".env"
    env_file.write_text("# comment\n\nRULE_AGENT_TIMEOUT=42\n", encoding="utf-8")
    load_local_env(env_file)
    assert os.environ["RULE_AGENT_TIMEOUT"] == "42"


def test_load_local_env_does_not_override_existing(tmp_dir, monkeypatch) -> None:
    monkeypatch.setenv("RULE_AGENT_TIMEOUT", "99")
    env_file = tmp_dir / ".env"
    env_file.write_text("RULE_AGENT_TIMEOUT=42\n", encoding="utf-8")
    load_local_env(env_file)
    assert os.environ["RULE_AGENT_TIMEOUT"] == "99"
