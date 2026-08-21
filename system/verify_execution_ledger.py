"""Verify the local execution adapter and idempotent execution ledger."""
from __future__ import annotations

import json
import pathlib
import shutil
import sqlite3
import sys

from common import sha256_file as digest

ROOT = pathlib.Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parent
SOURCE_DB = ROOT / "data" / "unified_semantics.sqlite3"
VERIFY_DIR = ROOT / "data" / ".verification"
TARGET_DB = VERIFY_DIR / "execution_ledger.test.sqlite3"


def verify() -> dict[str, object]:
    VERIFY_DIR.mkdir(parents=True, exist_ok=True)
    TARGET_DB.unlink(missing_ok=True)
    before = digest(SOURCE_DB)
    shutil.copy2(SOURCE_DB, TARGET_DB)
    sys.path.insert(0, str(PROJECT_ROOT / "backend"))
    from app import main as backend
    backend.UNIFIED_SEMANTICS_DB = TARGET_DB
    request = backend.SemanticExecutionDispatchRequest(
        actionRunId="verify-execution-run",
        adapterId="local-preview-adapter",
        idempotencyKey="verify-execution-ledger-v1",
        requestPayload={"mode": "preview", "target": "local semantic layer"},
    )
    first = backend.semantic_execution_dispatch(request)
    second = backend.semantic_execution_dispatch(request)
    db = sqlite3.connect(str(TARGET_DB))
    try:
        count = int(db.execute("SELECT count(*) FROM semantic_execution_ledger").fetchone()[0])
        unsafe = int(db.execute("SELECT count(*) FROM semantic_execution_ledger WHERE source_write<>0 OR formal_publication<>0").fetchone()[0])
        status = db.execute("SELECT status FROM semantic_execution_ledger WHERE idempotency_key=?", (request.idempotency_key,)).fetchone()[0]
    finally:
        db.close()
        TARGET_DB.unlink(missing_ok=True)
        backend.UNIFIED_SEMANTICS_DB = SOURCE_DB

    assert first["execution"]["status"] == "planned", first
    assert second["idempotentReplay"] is True, second
    assert count == 1, count
    assert status == "planned", status
    assert unsafe == 0, unsafe
    assert digest(SOURCE_DB) == before
    return {"status": "passed", "first_status": first["execution"]["status"], "idempotent_replay": second["idempotentReplay"], "ledger_count": count, "source_write": False, "formal_publication": False, "source_digest_unchanged": True}


if __name__ == "__main__":
    print(json.dumps(verify(), ensure_ascii=False, indent=2))
