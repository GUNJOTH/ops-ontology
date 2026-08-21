"""Verify rule-agent runtime safeguards without calling the external model."""
from __future__ import annotations

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parent
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

from app import main as backend  # noqa: E402


def verify() -> dict[str, object]:
    connection = backend.sqlite_connection()
    try:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(rule_agent_run)").fetchall()}
        required = {"error_code", "retryable", "attempt_count", "fallback_used"}
        missing = sorted(required - columns)
        if missing:
            raise AssertionError(f"rule_agent_run 缺少运行诊断字段: {missing}")

        code, retryable, attempts = backend.rule_agent_failure_fields(
            json.dumps({"code": "provider_http_503", "retryable": True, "attempts": 2})
        )
        assert code == "provider_http_503"
        assert retryable is True
        assert attempts == 2

        parsed = backend.parse_rule_agent_json('{"proposals": []}', "验证智能体")
        assert parsed == {"proposals": []}
        try:
            backend.parse_rule_agent_json("not-json", "验证智能体")
        except backend.RuleAgentCallError as exc:
            assert exc.code == "invalid_json"
            assert exc.retryable is True
        else:
            raise AssertionError("非法 JSON 未被隔离")

        status = backend.rule_agent_status()
        assert status["sourceWrite"] is False
        assert status["formalPublication"] is False
        return {
            "status": "passed",
            "diagnostic_columns": sorted(required),
            "configured": bool(status["configured"]),
            "failed_run_count": int(status["failedRunCount"]),
            "max_attempts": int(status["maxAttempts"]),
            "source_write": False,
            "formal_publication": False,
        }
    finally:
        connection.close()


if __name__ == "__main__":
    print(json.dumps(verify(), ensure_ascii=False, indent=2))
