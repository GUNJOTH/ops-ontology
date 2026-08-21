"""Put the 17 replay-passed keep-original members into formal approval queue."""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parent
DB = ROOT / "data" / "semantic_workflow.sqlite3"
REPLAY_ID = "replay-ai-cluster-keep-original-1a3e057aedab3c077b82"

BACKEND_ROOT = PROJECT_ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))
from app.main import ai_cluster_id, classify_ai_cluster, cluster_pattern  # noqa: E402


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def main() -> None:
    connection = sqlite3.connect(str(DB), timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS formal_approval_queue (
              queue_id TEXT PRIMARY KEY,
              candidate_id TEXT NOT NULL UNIQUE REFERENCES semantic_candidate(candidate_id),
              cluster_id TEXT NOT NULL,
              replay_id TEXT NOT NULL REFERENCES replay_run(replay_id),
              proposed_decision TEXT NOT NULL CHECK (proposed_decision IN ('approved','modified','rejected','deferred')),
              proposed_description TEXT NOT NULL,
              status TEXT NOT NULL CHECK (status IN ('pending','approved','modified','rejected','deferred')),
              note TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              source_write INTEGER NOT NULL DEFAULT 0 CHECK (source_write = 0),
              formal_publication INTEGER NOT NULL DEFAULT 0 CHECK (formal_publication = 0)
            );
            CREATE INDEX IF NOT EXISTS ix_formal_approval_queue_status ON formal_approval_queue(status, created_at);
            """
        )
        connection.commit()
        replay = connection.execute("SELECT * FROM replay_run WHERE replay_id=?", (REPLAY_ID,)).fetchone()
        if replay is None or replay["status"] != "passed" or replay["evaluation_count"] != 17 or replay["pass_count"] != 17 or replay["fail_count"] != 0:
            raise SystemExit("The 17-member keep-original replay gate is not satisfied.")

        confirmed = {
            row["cluster_id"]
            for row in connection.execute("SELECT cluster_id FROM ai_cluster_decision WHERE decision='keep_original'").fetchall()
        }
        source_rows = connection.execute(
            """
            SELECT c.candidate_id,c.batch_id,c.original_description,c.candidate_description,
              c.review_state,c.publication_state,c.validator_status,c.confidence,
              d.source_snapshot_id,d.source_schema,d.site_id,d.asset_number
            FROM semantic_candidate c
            JOIN device_identity d ON d.device_id=c.device_id
            WHERE c.review_state='pending' AND c.publication_state='unpublished'
              AND c.validator_status='candidate' AND c.confidence='high'
              AND c.evidence_level='strong'
              AND length(trim(c.original_description)) > 0
              AND length(trim(c.candidate_description)) > 0
              AND c.original_description <> c.candidate_description
            ORDER BY d.site_id,d.asset_number,c.candidate_id
            """
        ).fetchall()
        rows = []
        for row in source_rows:
            kind, _label, _recommendation, _confidence, _reason = classify_ai_cluster(row["original_description"], row["candidate_description"])
            cluster_id = ai_cluster_id(kind, cluster_pattern(kind, row["original_description"], row["candidate_description"]))
            if cluster_id in confirmed:
                rows.append((row, cluster_id))
        if len(rows) != 17:
            raise SystemExit(f"Expected 17 queue rows, found {len(rows)}")

        now = utc_now()
        connection.execute("BEGIN IMMEDIATE")
        inserted = 0
        for row, cluster_id in rows:
            queue_id = f"approval-ai-cluster-keep-original-{row['candidate_id']}"
            existing = connection.execute("SELECT status FROM formal_approval_queue WHERE candidate_id=?", (row["candidate_id"],)).fetchone()
            if existing is not None:
                if existing["status"] != "pending":
                    raise SystemExit(f"Candidate already has a completed approval state: {row['candidate_id']}")
                continue
            connection.execute(
                """
                INSERT INTO formal_approval_queue
                  (queue_id,candidate_id,cluster_id,replay_id,proposed_decision,proposed_description,status,note,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    queue_id,
                    row["candidate_id"],
                    cluster_id,
                    REPLAY_ID,
                    "modified",
                    row["original_description"],
                    "pending",
                    "AI 整簇驳回候选、保留原文；17/17 回放通过。正式审批时确认最终描述为原文。",
                    now,
                    now,
                ),
            )
            inserted += 1
        connection.execute(
            "INSERT INTO audit_event(entity_type,entity_id,event_type,actor,payload_json,event_at) VALUES (?,?,?,?,?,?)",
            (
                "formal_approval_queue",
                f"ai-cluster-keep-original-{REPLAY_ID}",
                "formal_approval_queue_created",
                "cluster-replay",
                '{"candidate_count":17,"cluster_count":7,"replay_id":"' + REPLAY_ID + '","source_write":false,"formal_publication":false}',
                now,
            ),
        )
        connection.commit()
        pending = connection.execute("SELECT count(*) FROM formal_approval_queue WHERE replay_id=? AND status='pending'", (REPLAY_ID,)).fetchone()[0]
        formal_reviews = connection.execute("SELECT count(*) FROM review_decision WHERE candidate_id IN (SELECT candidate_id FROM formal_approval_queue WHERE replay_id=?)", (REPLAY_ID,)).fetchone()[0]
        publications = connection.execute("SELECT count(*) FROM published_description WHERE candidate_id IN (SELECT candidate_id FROM formal_approval_queue WHERE replay_id=?)", (REPLAY_ID,)).fetchone()[0]
        print({"queued": inserted, "pending": pending, "formal_reviews_created": formal_reviews, "published": publications, "source_write": False, "formal_publication": False})
    finally:
        connection.close()


if __name__ == "__main__":
    main()
