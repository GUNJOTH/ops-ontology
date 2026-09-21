"""单元测试：app.core.utils 的确定性辅助函数。"""
from __future__ import annotations

import hashlib
from datetime import datetime

import pytest
from app.core.utils import (
    decode_json_value,
    ontology_trace_id,
    parse_json_array,
    sha256_file,
    utc_now,
)

pytestmark = pytest.mark.unit


def test_utc_now_is_iso8601_with_utc_offset() -> None:
    value = utc_now()
    parsed = datetime.fromisoformat(value)
    assert parsed.tzinfo is not None
    assert value.endswith("+00:00")


def test_parse_json_array_empty_and_none() -> None:
    assert parse_json_array(None) == []
    assert parse_json_array("") == []
    assert parse_json_array("   ") == []


def test_parse_json_array_valid_list_coerces_items_to_str() -> None:
    assert parse_json_array('["a", 1, true]') == ["a", "1", "True"]


def test_parse_json_array_invalid_returns_empty() -> None:
    assert parse_json_array("not-json") == []


def test_parse_json_array_non_list_returns_empty() -> None:
    assert parse_json_array('{"a": 1}') == []
    assert parse_json_array("42") == []


def test_decode_json_value_roundtrip() -> None:
    assert decode_json_value('{"a": 1}') == {"a": 1}
    assert decode_json_value("[1,2]") == [1, 2]
    assert decode_json_value("null") is None


def test_decode_json_value_fallback() -> None:
    assert decode_json_value("bad", fallback={"x": 1}) == {"x": 1}
    assert decode_json_value(None, fallback=[]) == []
    assert decode_json_value("", fallback="default") == "default"


def test_ontology_trace_id_deterministic_and_prefixed() -> None:
    first = ontology_trace_id("device", "abc")
    second = ontology_trace_id("device", "abc")
    assert first == second
    assert first.startswith("device-")
    assert len(first) == len("device") + 1 + 24


def test_ontology_trace_id_differs_by_value_and_prefix() -> None:
    assert ontology_trace_id("device", "a") != ontology_trace_id("device", "b")
    assert ontology_trace_id("device", "a") != ontology_trace_id("location", "a")


def test_sha256_file_matches_stdlib(tmp_dir) -> None:
    path = tmp_dir / "sample.txt"
    path.write_text("hello", encoding="utf-8")
    expected = hashlib.sha256(b"hello").hexdigest()
    assert sha256_file(path) == expected
    assert len(sha256_file(path)) == 64
