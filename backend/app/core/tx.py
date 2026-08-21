"""Small transaction primitives shared by write-side domain services."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from collections.abc import Iterator


@contextmanager
def write_transaction(connection: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run a local write unit atomically with an immediate SQLite lock."""
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield connection
        connection.commit()
    except BaseException:
        if connection.in_transaction:
            connection.rollback()
        raise


def begin_write(connection: sqlite3.Connection) -> None:
    """Compatibility primitive for legacy handlers being migrated gradually."""
    connection.execute("BEGIN IMMEDIATE")


def commit_write(connection: sqlite3.Connection) -> None:
    connection.commit()

