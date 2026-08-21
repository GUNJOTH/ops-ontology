"""数据测试：app.core.tx 的事务原语。"""
from __future__ import annotations

import pytest

from app.core.tx import begin_write, commit_write, write_transaction

pytestmark = pytest.mark.unit


@pytest.fixture
def kv_table(tmp_workflow_db):
    tmp_workflow_db.execute("CREATE TABLE kv (k TEXT PRIMARY KEY, v TEXT NOT NULL)")
    return tmp_workflow_db


def test_write_transaction_commits_on_success(kv_table) -> None:
    with write_transaction(kv_table) as connection:
        connection.execute("INSERT INTO kv(k, v) VALUES ('a', '1')")
    row = kv_table.execute("SELECT v FROM kv WHERE k='a'").fetchone()
    assert row["v"] == "1"


def test_write_transaction_rolls_back_on_error(kv_table) -> None:
    with pytest.raises(RuntimeError):
        with write_transaction(kv_table) as connection:
            connection.execute("INSERT INTO kv(k, v) VALUES ('a', '1')")
            raise RuntimeError("boom")
    assert kv_table.execute("SELECT count(*) AS c FROM kv").fetchone()["c"] == 0


def test_begin_and_commit_write(kv_table) -> None:
    begin_write(kv_table)
    kv_table.execute("INSERT INTO kv(k, v) VALUES ('b', '2')")
    commit_write(kv_table)
    assert kv_table.execute("SELECT count(*) AS c FROM kv").fetchone()["c"] == 1
