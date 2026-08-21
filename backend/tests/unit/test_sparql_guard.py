"""单元测试：app.core.sparql_guard.guard_text 的掩码行为。"""
from __future__ import annotations

import pytest

from app.core.sparql_guard import guard_text

pytestmark = pytest.mark.unit


def test_guard_text_preserves_plain_query() -> None:
    query = "SELECT * WHERE { ?s ?p ?o }"
    assert guard_text(query) == query


def test_guard_text_masks_comment() -> None:
    query = "SELECT * WHERE { ?s ?p ?o } # DELETE all"
    masked = guard_text(query)
    assert "DELETE" not in masked
    assert "SELECT" in masked


def test_guard_text_masks_double_quoted_literal() -> None:
    query = 'SELECT * WHERE { ?s ?p "DELETE me" }'
    masked = guard_text(query)
    assert "DELETE" not in masked


def test_guard_text_masks_single_quoted_literal() -> None:
    query = "SELECT * WHERE { ?s ?p 'INSERT here' }"
    masked = guard_text(query)
    assert "INSERT" not in masked


def test_guard_text_masks_triple_quoted_literal() -> None:
    query = 'SELECT * WHERE { ?s ?p """LOAD DATA""" }'
    masked = guard_text(query)
    assert "LOAD" not in masked


def test_guard_text_masks_iri() -> None:
    query = "SELECT * WHERE { <http://example.org/DELETE> ?p ?o }"
    masked = guard_text(query)
    assert "DELETE" not in masked


def test_guard_text_keeps_keyword_outside_literal() -> None:
    query = 'SELECT * WHERE { ?s ?p "x" } DELETE WHERE { ?s ?p ?o }'
    masked = guard_text(query)
    assert "DELETE" in masked


def test_guard_text_masks_escaped_quote_literal() -> None:
    query = 'SELECT * WHERE { ?s ?p "a\\"DELETE" }'
    masked = guard_text(query)
    assert "DELETE" not in masked
