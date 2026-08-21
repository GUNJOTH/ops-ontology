"""Verify the canonical defect-state gate against a temporary local overlay."""
from __future__ import annotations

import json
import pathlib
import sqlite3
import sys


ROOT = pathlib.Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parent
SOURCE_DB = ROOT / "data" / "unified_semantics.sqlite3"
BACKEND_ROOT = PROJECT_ROOT / "backend"
VERIFY_ROOT = ROOT / "data" / ".verification"


def matching_status_id(path: pathlib.Path) -> str:
    db = sqlite3.connect(str(path))
    db.row_factory = sqlite3.Row
    try:
        mappings = {
            (row["source_schema"], row["source_table"], row["raw_status"], row["source_snapshot_id"]): row["status_id"]
            for row in db.execute("SELECT * FROM semantic_status_dictionary")
        }
        for fact in db.execute("SELECT * FROM semantic_fact WHERE fact_type='defect_status'"):
            payload = json.loads(fact["value_json"])
            key = (
                fact["source_schema"],
                fact["source_table"],
                payload.get("raw_status") or "",
                fact["source_snapshot_id"],
            )
            if key in mappings:
                return mappings[key]
    finally:
        db.close()
    raise AssertionError("没有找到可用于状态回放验证的缺陷状态事实")


def verify() -> dict[str, object]:
    if not SOURCE_DB.exists():
        raise AssertionError(f"本地语义覆盖层不存在: {SOURCE_DB}")
    sys.path.insert(0, str(BACKEND_ROOT))
    from app import main as backend

    VERIFY_ROOT.mkdir(parents=True, exist_ok=True)
    target = VERIFY_ROOT / "world_model_p0.test.sqlite3"
    if not target.resolve().is_relative_to(VERIFY_ROOT.resolve()):
        raise AssertionError("临时验证文件超出项目边界")
    target.unlink(missing_ok=True)
    source = sqlite3.connect(f"file:{SOURCE_DB.resolve()}?mode=ro", uri=True)
    clone = sqlite3.connect(str(target))
    try:
        source.backup(clone)
    finally:
        clone.close()
        source.close()
    try:
        backend.UNIFIED_SEMANTICS_DB = target
        status_id = matching_status_id(target)
        db = sqlite3.connect(str(target))
        try:
            previous_version = int(db.execute(
                "SELECT mapping_version FROM semantic_status_dictionary WHERE status_id=?",
                (status_id,),
            ).fetchone()[0])
            previous_audit_count = int(db.execute(
                "SELECT count(*) FROM semantic_status_mapping_review WHERE status_id=?",
                (status_id,),
            ).fetchone()[0])
        finally:
            db.close()
        review = backend.review_semantic_status_dictionary(
            status_id,
            backend.DefectStatusReviewRequest(
                decision="approved",
                canonicalState="UNKNOWN",
                businessMeaning="未知",
                notes="自动化验证：仅操作临时覆盖库",
                reviewer="verify_world_model_p0",
            ),
        )
        replay = backend.replay_semantic_status_dictionary()
        summary = backend.world_model_summary()
        db = sqlite3.connect(str(target))
        try:
            audit_count = int(db.execute(
                "SELECT count(*) FROM semantic_status_mapping_review WHERE status_id=?",
                (status_id,),
            ).fetchone()[0])
            canonical_fact_count = int(db.execute(
                "SELECT count(*) FROM semantic_fact WHERE fact_type='canonical_defect_state' AND status='derived'"
            ).fetchone()[0])
            action_count = int(db.execute("SELECT count(*) FROM semantic_escalation_action").fetchone()[0])
        finally:
            db.close()

        assert review["item"]["canonical_state"] == "UNKNOWN"
        assert int(review["item"]["mapping_version"]) == previous_version + 1
        assert audit_count == previous_audit_count + 1
        assert replay["status"] == "completed"
        assert int(replay["matched_fact_count"]) > 0
        assert canonical_fact_count > 0
        assert int(replay["action_count"]) == 0
        assert action_count == 0
        assert replay["source_write"] is False
        assert replay["formal_publication"] is False
        assert summary["sourceWrite"] is False
        result = {
            "status": "passed",
            "reviewed_status_id": status_id,
            "previous_mapping_version": previous_version,
            "mapping_version": review["item"]["mapping_version"],
            "previous_audit_count": previous_audit_count,
            "audit_count": audit_count,
            "matched_fact_count": replay["matched_fact_count"],
            "canonical_fact_count": canonical_fact_count,
            "action_count": action_count,
            "source_write": False,
            "formal_publication": False,
        }
    finally:
        backend.UNIFIED_SEMANTICS_DB = SOURCE_DB
        target.unlink(missing_ok=True)
    return result


if __name__ == "__main__":
    print(json.dumps(verify(), ensure_ascii=False, indent=2))
