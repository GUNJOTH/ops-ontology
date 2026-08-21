"""OpenAI-compatible model gateway with bounded, auditable failures."""
from __future__ import annotations

import json
import re
import time
from typing import Any

from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAI

from app.core.config import (
    RULE_AGENT_API_KEY,
    RULE_AGENT_BASE_URL,
    RULE_AGENT_MAX_ATTEMPTS,
    RULE_AGENT_MAX_TOKENS,
    RULE_AGENT_MODEL,
    RULE_AGENT_RETRY_BACKOFF,
    RULE_AGENT_THINKING,
    RULE_AGENT_TIMEOUT,
)


class RuleAgentCallError(RuntimeError):
    """A model-call failure with a stable code for audit and retry policy."""

    def __init__(self, code: str, message: str, retryable: bool, attempts: int) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.attempts = attempts


def invoke_rule_agent_completion(messages: list[dict[str, str]], task_label: str) -> str:
    """Call the configured gateway without allowing model calls to publish data."""
    if not RULE_AGENT_API_KEY or not RULE_AGENT_BASE_URL or not RULE_AGENT_MODEL:
        raise RuleAgentCallError("agent_not_configured", f"{task_label}尚未配置模型环境", False, 0)
    base_url = RULE_AGENT_BASE_URL.rstrip("/")
    if not base_url.endswith("/v1"):
        base_url += "/v1"
    last_error: RuleAgentCallError | None = None
    for attempt in range(1, RULE_AGENT_MAX_ATTEMPTS + 1):
        try:
            client = OpenAI(
                base_url=base_url,
                api_key=RULE_AGENT_API_KEY,
                timeout=RULE_AGENT_TIMEOUT,
                max_retries=0,
            )
            completion = client.chat.completions.create(
                model=RULE_AGENT_MODEL,
                max_tokens=RULE_AGENT_MAX_TOKENS,
                temperature=0,
                extra_body={"thinking": {"type": RULE_AGENT_THINKING}},
                messages=messages,
            )
            text = completion.choices[0].message.content if completion.choices else ""
            if isinstance(text, list):
                text = "".join(item.get("text", "") for item in text if isinstance(item, dict))
            text = str(text or "").strip()
            if text:
                return text
            last_error = RuleAgentCallError("empty_response", f"{task_label}未返回内容", True, attempt)
        except APIStatusError as exc:
            status = int(getattr(exc, "status_code", 0) or 0)
            retryable = status in {408, 409, 425, 429} or status >= 500
            last_error = RuleAgentCallError(
                f"provider_http_{status or 'unknown'}",
                f"{task_label}调用失败：HTTP {status or 'unknown'}",
                retryable,
                attempt,
            )
        except (APIConnectionError, APITimeoutError, TimeoutError) as exc:
            last_error = RuleAgentCallError(
                f"provider_{type(exc).__name__.lower()}",
                f"{task_label}调用失败：{type(exc).__name__}",
                True,
                attempt,
            )
        except Exception as exc:
            last_error = RuleAgentCallError(
                f"provider_{type(exc).__name__.lower()}",
                f"{task_label}调用失败：{type(exc).__name__}",
                False,
                attempt,
            )
        if last_error is not None and (not last_error.retryable or attempt >= RULE_AGENT_MAX_ATTEMPTS):
            raise last_error
        time.sleep(RULE_AGENT_RETRY_BACKOFF * attempt)
    raise last_error or RuleAgentCallError("unknown_agent_error", f"{task_label}调用失败", False, RULE_AGENT_MAX_ATTEMPTS)


def parse_rule_agent_json(text: str, task_label: str) -> Any:
    """Parse JSON-only model output, allowing one harmless Markdown wrapper."""
    cleaned = re.sub(r"^```(?:json)?\s*", "", text.strip(), flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned, flags=re.IGNORECASE)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start < 0 or end <= start:
            raise RuleAgentCallError("invalid_json", f"{task_label}返回的不是有效 JSON", True, 1)
        try:
            return json.loads(cleaned[start : end + 1])
        except json.JSONDecodeError as exc:
            raise RuleAgentCallError("invalid_json", f"{task_label}返回的不是有效 JSON", True, 1) from exc


def rule_agent_failure_detail(exc: RuleAgentCallError) -> str:
    return json.dumps(
        {"code": exc.code, "message": exc.message, "retryable": exc.retryable, "attempts": exc.attempts},
        ensure_ascii=False,
    )


def rule_agent_failure_fields(detail: object) -> tuple[str, bool, int]:
    """Read the stable error envelope without exposing request credentials."""
    try:
        payload = json.loads(str(detail))
    except (TypeError, json.JSONDecodeError):
        return "unknown_agent_error", False, 0
    if not isinstance(payload, dict):
        return "unknown_agent_error", False, 0
    return (
        str(payload.get("code") or "unknown_agent_error"),
        bool(payload.get("retryable")),
        max(0, int(payload.get("attempts") or 0)),
    )

