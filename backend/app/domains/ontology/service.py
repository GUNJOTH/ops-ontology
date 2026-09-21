"""Ontology meta-model helpers."""
from __future__ import annotations

import sqlite3


def ontology_meta_tables(connection: sqlite3.Connection) -> set[str]:
    return {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}

