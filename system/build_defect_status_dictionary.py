"""Build a reviewable HD/XNY defect-status dictionary from read-only evidence."""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import sqlite3
from datetime import datetime, timezone

from pipeline.contracts import connect_readonly

ROOT = pathlib.Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parent
DEFAULT_TARGET = ROOT / "data" / "unified_semantics.sqlite3"
IDENTITY_RESULT_ROOT = PROJECT_ROOT / "pilots" / "identity" / "results"

CANONICAL_DEFECT_STATES: tuple[tuple[str, str, str, int, int], ...] = (
    ("NEW", "新建", "缺陷已登记，尚未进入处理流程。", 0, 10),
    ("PENDING", "待处理", "缺陷等待受理、分派或开始处理。", 0, 20),
    ("PROCESSING", "处理中", "缺陷正在处置或消缺。", 0, 30),
    ("RESOLVED", "已解决", "处置已完成，但可能仍等待验收或正式关闭。", 0, 40),
    ("CLOSED", "已关闭", "缺陷流程已完成并关闭。", 1, 50),
    ("CANCELLED", "已取消", "缺陷已撤销或取消，不再继续处理。", 1, 60),
    ("SUSPENDED", "已挂起", "缺陷处理被暂停，等待后续条件。", 0, 70),
    ("UNKNOWN", "未知", "现有证据不足，暂时不能确定标准业务状态。", 0, 80),
)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sid(prefix: str, *parts: object) -> str:
    raw = "|".join("" if part is None else str(part) for part in parts)
    return f"{prefix}-{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:24]}"


def latest_identity_db() -> pathlib.Path:
    databases = sorted(IDENTITY_RESULT_ROOT.glob("identity-layer-v1-*/identity_semantics.sqlite3"), reverse=True)
    if not databases:
        raise RuntimeError("没有找到身份语义只读快照")
    return databases[0]


def ensure_schema(db: sqlite3.Connection) -> None:
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS semantic_status_dictionary (
          status_id TEXT PRIMARY KEY,
          source_schema TEXT NOT NULL,
          source_table TEXT NOT NULL,
          raw_status TEXT NOT NULL,
          status_present INTEGER NOT NULL CHECK (status_present IN (0,1)),
          evidence_count INTEGER NOT NULL,
          linked_device_evidence_count INTEGER NOT NULL,
          example_records_json TEXT NOT NULL,
          source_snapshot_id TEXT NOT NULL,
          mapping_status TEXT NOT NULL CHECK (mapping_status IN ('pending','approved','rejected')),
          business_meaning TEXT,
          mapping_notes TEXT,
          reviewer TEXT,
          reviewed_at TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          UNIQUE(source_schema,source_table,raw_status,source_snapshot_id)
        );
        CREATE TABLE IF NOT EXISTS semantic_status_dictionary_run (
          run_id TEXT PRIMARY KEY,
          source_snapshot_id TEXT NOT NULL,
          candidate_count INTEGER NOT NULL,
          pending_count INTEGER NOT NULL,
          approved_count INTEGER NOT NULL,
          rejected_count INTEGER NOT NULL,
          evidence_row_count INTEGER NOT NULL,
          source_write INTEGER NOT NULL CHECK (source_write=0),
          formal_publication INTEGER NOT NULL CHECK (formal_publication=0),
          created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_semantic_status_dictionary_filter
          ON semantic_status_dictionary(source_schema,source_table,mapping_status);
        CREATE INDEX IF NOT EXISTS ix_semantic_status_dictionary_raw
          ON semantic_status_dictionary(raw_status);
        CREATE TABLE IF NOT EXISTS semantic_canonical_state (
          state_domain TEXT NOT NULL,
          canonical_state TEXT NOT NULL,
          display_name TEXT NOT NULL,
          description TEXT NOT NULL,
          is_terminal INTEGER NOT NULL CHECK (is_terminal IN (0,1)),
          sort_order INTEGER NOT NULL,
          status TEXT NOT NULL CHECK (status IN ('active','inactive')),
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          PRIMARY KEY(state_domain,canonical_state)
        );
        CREATE TABLE IF NOT EXISTS semantic_status_mapping_review (
          review_id TEXT PRIMARY KEY,
          status_id TEXT NOT NULL REFERENCES semantic_status_dictionary(status_id),
          mapping_version INTEGER NOT NULL,
          previous_mapping_status TEXT,
          previous_canonical_state TEXT,
          decision TEXT NOT NULL CHECK (decision IN ('approved','rejected')),
          canonical_state TEXT,
          business_meaning TEXT,
          notes TEXT NOT NULL,
          reviewer TEXT NOT NULL,
          reviewed_at TEXT NOT NULL,
          source_write INTEGER NOT NULL CHECK (source_write=0),
          formal_publication INTEGER NOT NULL CHECK (formal_publication=0),
          UNIQUE(status_id,mapping_version)
        );
        CREATE INDEX IF NOT EXISTS ix_semantic_status_mapping_review_status
          ON semantic_status_mapping_review(status_id,mapping_version DESC);
        """
    )
    columns = {row[1] for row in db.execute("PRAGMA table_info(semantic_status_dictionary)")}
    if "canonical_state" not in columns:
        db.execute("ALTER TABLE semantic_status_dictionary ADD COLUMN canonical_state TEXT")
    if "mapping_version" not in columns:
        db.execute("ALTER TABLE semantic_status_dictionary ADD COLUMN mapping_version INTEGER NOT NULL DEFAULT 0")
    created = now()
    db.executemany(
        """
        INSERT INTO semantic_canonical_state(
          state_domain,canonical_state,display_name,description,is_terminal,
          sort_order,status,created_at,updated_at
        ) VALUES ('DEFECT',?,?,?,?,?,'active',?,?)
        ON CONFLICT(state_domain,canonical_state) DO UPDATE SET
          display_name=excluded.display_name,
          description=excluded.description,
          is_terminal=excluded.is_terminal,
          sort_order=excluded.sort_order,
          status='active',
          updated_at=excluded.updated_at
        """,
        [(*state, created, created) for state in CANONICAL_DEFECT_STATES],
    )


def build(target_path: pathlib.Path, identity_path: pathlib.Path | None = None) -> dict[str, object]:
    identity_path = identity_path or latest_identity_db()
    source = connect_readonly(identity_path, timeout=30)
    try:
        source_rows = source.execute(
            """
            SELECT source_schema,source_table,coalesce(status,'') AS raw_status,
              source_snapshot_id,count(*) AS evidence_count,
              sum(CASE WHEN link_status='accepted' THEN 1 ELSE 0 END) AS linked_device_evidence_count
            FROM device_event
            WHERE event_type='defect'
            GROUP BY source_schema,source_table,coalesce(status,''),source_snapshot_id
            ORDER BY source_schema,source_table,raw_status
            """
        ).fetchall()
        examples: dict[tuple[str, str, str, str], list[dict[str, object]]] = {}
        for row in source_rows:
            key = (row["source_schema"], row["source_table"], row["raw_status"], row["source_snapshot_id"])
            examples[key] = [dict(item) for item in source.execute(
                """
                SELECT event_record_id,source_row_id,site_id,location_code,status,description,
                  link_status,source_snapshot_id
                FROM device_event
                WHERE event_type='defect' AND source_schema=? AND source_table=?
                  AND coalesce(status,'')=? AND source_snapshot_id=?
                ORDER BY source_row_id LIMIT 3
                """,
                key,
            ).fetchall()]
    finally:
        source.close()

    db = sqlite3.connect(str(target_path), timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout=30000")
    ensure_schema(db)
    created = now()
    for row in source_rows:
        key = (row["source_schema"], row["source_table"], row["raw_status"], row["source_snapshot_id"])
        status_id = sid("SDS", *key)
        linked_count = int(row["linked_device_evidence_count"] or 0)
        db.execute(
            """
            INSERT INTO semantic_status_dictionary(
              status_id,source_schema,source_table,raw_status,status_present,
              evidence_count,linked_device_evidence_count,example_records_json,
              source_snapshot_id,mapping_status,business_meaning,mapping_notes,
              reviewer,reviewed_at,created_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,'pending',NULL,NULL,NULL,NULL,?,?)
            ON CONFLICT(source_schema,source_table,raw_status,source_snapshot_id) DO UPDATE SET
              evidence_count=excluded.evidence_count,
              linked_device_evidence_count=excluded.linked_device_evidence_count,
              example_records_json=excluded.example_records_json,
              updated_at=excluded.updated_at
            """,
            (
                status_id, row["source_schema"], row["source_table"], row["raw_status"],
                1 if str(row["raw_status"]).strip() else 0, row["evidence_count"], linked_count,
                json.dumps(examples[key], ensure_ascii=False), row["source_snapshot_id"], created, created,
            ),
        )
    source_snapshot_id = source_rows[0]["source_snapshot_id"] if source_rows else ""
    counts = {
        "candidate_count": int(db.execute("SELECT count(*) FROM semantic_status_dictionary").fetchone()[0]),
        "pending_count": int(db.execute("SELECT count(*) FROM semantic_status_dictionary WHERE mapping_status='pending'").fetchone()[0]),
        "approved_count": int(db.execute("SELECT count(*) FROM semantic_status_dictionary WHERE mapping_status='approved'").fetchone()[0]),
        "rejected_count": int(db.execute("SELECT count(*) FROM semantic_status_dictionary WHERE mapping_status='rejected'").fetchone()[0]),
        "evidence_row_count": int(db.execute("SELECT coalesce(sum(evidence_count),0) FROM semantic_status_dictionary").fetchone()[0]),
    }
    run_id = sid("SDSR", source_snapshot_id, created)
    db.execute(
        """
        INSERT INTO semantic_status_dictionary_run(
          run_id,source_snapshot_id,candidate_count,pending_count,approved_count,
          rejected_count,evidence_row_count,source_write,formal_publication,created_at
        ) VALUES (?,?,?,?,?,?,?,0,0,?)
        """,
        (run_id, source_snapshot_id, counts["candidate_count"], counts["pending_count"], counts["approved_count"], counts["rejected_count"], counts["evidence_row_count"], created),
    )
    db.commit()
    db.close()
    return {"run_id": run_id, "source_snapshot_id": source_snapshot_id, **counts, "source_write": False, "formal_publication": False}


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a reviewable defect-status dictionary")
    parser.add_argument("--target-db", type=pathlib.Path, default=DEFAULT_TARGET)
    parser.add_argument("--identity-db", type=pathlib.Path)
    args = parser.parse_args()
    print(json.dumps(build(args.target_db.resolve(), args.identity_db.resolve() if args.identity_db else None), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
