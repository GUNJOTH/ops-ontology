"""Orchestrate local workflow migrations at application startup only.

The two existing schema builders are passed in by the compatibility layer for
now. Keeping this orchestration separate makes the lifecycle explicit and
allows the SQL bodies to be moved here incrementally without changing routes.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import datetime, timezone

SchemaStep = Callable[[sqlite3.Connection], None]
MigrationInput = SchemaStep | tuple[str, SchemaStep]


def migrate_workflow_schema(connection: sqlite3.Connection, *steps: MigrationInput) -> None:
    """Run versioned local schema steps once during application startup.

    Existing callers may still pass a function.  New production migrations
    should pass ``(migration_id, function)`` so a schema change gets a new,
    auditable version instead of relying on a repeated ``CREATE IF NOT EXISTS``.
    """
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS semantic_schema_migration (
          migration_id TEXT PRIMARY KEY,
          applied_at TEXT NOT NULL
        )
        """
    )
    for index, item in enumerate(steps, start=1):
        if isinstance(item, tuple):
            migration_id, step = item
        else:
            step = item
            migration_id = f"workflow-step-v1-{index}-{step.__module__}.{step.__qualname__}"
        if connection.execute(
            "SELECT 1 FROM semantic_schema_migration WHERE migration_id=?",
            (migration_id,),
        ).fetchone():
            continue
        step(connection)
        connection.execute(
            "INSERT INTO semantic_schema_migration(migration_id, applied_at) VALUES (?, ?)",
            (migration_id, datetime.now(timezone.utc).isoformat()),
        )
    connection.commit()
