"""Read-only SQLite diagnostics under the shared pipeline run envelope."""
from __future__ import annotations

import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DB = ROOT / "data" / "semantic_workflow.sqlite3"


def main() -> dict[str, object]:
    result: dict[str, object] = {
        "path": str(DB),
        "exists": DB.exists(),
        "size": DB.stat().st_size if DB.exists() else 0,
    }
    for mode, target, uri in [
        ("normal", str(DB), False),
        ("immutable", f"file:{DB.as_posix()}?mode=ro&immutable=1", True),
    ]:
        try:
            connection = sqlite3.connect(target, uri=uri, timeout=30)
            result[mode] = {
                "journal_mode": connection.execute("PRAGMA journal_mode").fetchone()[0],
                "integrity": connection.execute("PRAGMA integrity_check").fetchone()[0],
                "devices": connection.execute("SELECT count(*) FROM device_identity").fetchone()[0],
                "candidates": connection.execute("SELECT count(*) FROM semantic_candidate").fetchone()[0],
            }
            connection.close()
        except Exception as exc:  # diagnostic output only
            result[mode] = {"error": f"{type(exc).__name__}: {exc}"}
    result.update({"sourceWrite": False, "formalPublication": False})
    return result


if __name__ == "__main__":
    from pipeline.legacy import run_legacy_main

    raise SystemExit(
        run_legacy_main(
            pipeline_id="diagnose-sqlite",
            pipeline_version="v1",
            root=ROOT,
            legacy_main=main,
        )
    )
