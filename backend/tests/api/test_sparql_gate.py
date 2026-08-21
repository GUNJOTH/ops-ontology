"""API 测试：/api/semantic/sparql 只读门禁。

门禁在访问 Canonical 库之前执行，因此这些用例无需真实数据即可运行。
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.main import CanonicalSparqlRequest, canonical_semantic_sparql

pytestmark = pytest.mark.api


@pytest.mark.parametrize(
    "query",
    [
        "INSERT DATA { <s> <p> <o> }",
        "DELETE WHERE { ?s ?p ?o }",
        "SELECT * WHERE { SERVICE <http://example.org> { ?s ?p ?o } }",
        "LOAD <http://example.org/data>",
        "CLEAR GRAPH <http://example.org/g>",
        "DROP GRAPH <http://example.org/g>",
        "CREATE GRAPH <http://example.org/g>",
        "UPDATE WHERE { ?s ?p ?o }",
        "CONSTRUCT { ?s ?p ?o } WHERE { ?s ?p ?o }",
        "DESCRIBE <http://example.org/s>",
    ],
)
def test_sparql_gate_rejects_non_readonly(query: str) -> None:
    with pytest.raises(HTTPException) as excinfo:
        canonical_semantic_sparql(CanonicalSparqlRequest(query=query))
    assert excinfo.value.status_code == 422


def test_sparql_gate_allows_readonly_select(monkeypatch) -> None:
    """只读 SELECT 通过门禁，到达建库步骤（用哨兵异常证明门禁放行）。"""

    def fake_connection():
        raise RuntimeError("reached-canonical-db")

    monkeypatch.setattr("app.main.canonical_semantics_connection", fake_connection)
    with pytest.raises(RuntimeError, match="reached-canonical-db"):
        canonical_semantic_sparql(
            CanonicalSparqlRequest(query="SELECT * WHERE { ?s ?p ?o }")
        )
