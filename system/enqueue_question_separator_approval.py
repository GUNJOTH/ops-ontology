"""Queue the replay-passed question-separator preview for formal approval.

This script only creates pending approval-queue rows. It does not change
semantic candidate states, source data, review decisions, or publication data.
"""
from __future__ import annotations

import csv
import json
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parent
DB = ROOT / "data" / "semantic_workflow.sqlite3"
PREVIEW_DIR = PROJECT_ROOT / "pilots" / "HD_SAAS" / "question_separator_preview"
PREVIEW_CSV = PREVIEW_DIR / "rewrite_preview.csv"
REPLAY_MANIFEST = PREVIEW_DIR / "replay_manifest.json"
RULE_VERSION = "question-separator-proposed-20260813-v1"
RULE_KEY = "semantic.separator.fullwidth_question_mark_to_space"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def main() -> None:
    if not PREVIEW_CSV.exists() or not REPLAY_MANIFEST.exists():
        raise SystemExit("Question separator preview or replay manifest is missing.")
    replay = json.loads(REPLAY_MANIFEST.read_text(encoding="utf-8"))
    if replay.get("status") != "passed" or replay.get("evaluation_count") != 477 or replay.get("pass_count") != 477 or replay.get("fail_count") != 0:
        raise SystemExit("Question separator replay gate is not satisfied: expected 477/477 passed.")
    if replay.get("rule_version") != RULE_VERSION or replay.get("rule_key") != RULE_KEY:
        raise SystemExit("Replay rule version or key does not match queue rule.")

    with PREVIEW_CSV.open(encoding="utf-8-sig", newline="") as handle:
        preview_rows = list(csv.DictReader(handle))
    if len(preview_rows) != 477 or len({row["CANDIDATE_ID"] for row in preview_rows}) != 477:
        raise SystemExit("Question separator preview must contain 477 unique rows.")

    connection = sqlite3.connect(str(DB), timeout=60)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=60000")
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
        replay_row = connection.execute("SELECT * FROM replay_run WHERE replay_id=?", (replay["replay_id"],)).fetchone()
        if replay_row is None or replay_row["status"] != "passed":
            raise SystemExit("Replay run is not present or not passed in SQLite.")

        candidate_ids = [row["CANDIDATE_ID"] for row in preview_rows]
        placeholders = ",".join("?" for _ in candidate_ids)
        db_rows = connection.execute(
            f"""
            SELECT c.candidate_id,c.review_state,c.publication_state,c.validator_status,c.confidence,
                   c.original_description,c.candidate_description,d.site_id,d.asset_number
            FROM semantic_candidate c
            JOIN device_identity d ON d.device_id=c.device_id
            WHERE c.candidate_id IN ({placeholders})
            """,
            candidate_ids,
        ).fetchall()
        db_by_id = {row["candidate_id"]: row for row in db_rows}
        if len(db_by_id) != 477:
            raise SystemExit(f"Preview/database candidate mismatch: {len(db_by_id)}/477.")
        for preview in preview_rows:
            row = db_by_id[preview["CANDIDATE_ID"]]
            if row["review_state"] != "pending" or row["publication_state"] != "unpublished":
                raise SystemExit(f"Candidate is no longer pending/unpublished: {preview['CANDIDATE_ID']}")
            if row["validator_status"] != "candidate" or row["confidence"] != "high":
                raise SystemExit(f"Candidate quality gate changed: {preview['CANDIDATE_ID']}")

        now = utc_now()
        connection.execute("BEGIN IMMEDIATE")
        inserted = 0
        already_pending = 0
        class_counts: Counter[str] = Counter()
        site_counts: Counter[str] = Counter()
        for preview in preview_rows:
            candidate_id = preview["CANDIDATE_ID"]
            existing = connection.execute("SELECT status FROM formal_approval_queue WHERE candidate_id=?", (candidate_id,)).fetchone()
            if existing is not None:
                if existing["status"] != "pending":
                    raise SystemExit(f"Candidate already has a completed approval state: {candidate_id}")
                already_pending += 1
                continue
            class_name = preview["QUESTION_MARK_CLASS"]
            cluster_id = f"question-separator-{class_name}"
            connection.execute(
                """
                INSERT INTO formal_approval_queue
                  (queue_id,candidate_id,cluster_id,replay_id,proposed_decision,proposed_description,status,note,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    f"approval-question-separator-{candidate_id}",
                    candidate_id,
                    cluster_id,
                    replay["replay_id"],
                    "modified",
                    preview["PROPOSED_DESCRIPTION"],
                    "pending",
                    f"问号分隔符规则待正式审批；{RULE_KEY}；{replay['pass_count']}/{replay['evaluation_count']} 回放通过；当前仅生成审批凭据，不发布。",
                    now,
                    now,
                ),
            )
            inserted += 1
            class_counts[class_name] += 1
            site_counts[preview["SITEID"] or "(空)"] += 1

        connection.execute(
            "INSERT INTO audit_event(entity_type,entity_id,event_type,actor,payload_json,event_at) VALUES (?,?,?,?,?,?)",
            (
                "formal_approval_queue",
                f"question-separator-{replay['replay_id']}",
                "formal_approval_queue_created",
                "question-separator-replay",
                json.dumps({
                    "candidate_count": len(preview_rows),
                    "inserted": inserted,
                    "already_pending": already_pending,
                    "rule_key": RULE_KEY,
                    "rule_version": RULE_VERSION,
                    "replay_id": replay["replay_id"],
                    "source_write": False,
                    "formal_publication": False,
                }, ensure_ascii=False),
                now,
            ),
        )
        connection.commit()
        pending = connection.execute("SELECT count(*) FROM formal_approval_queue WHERE replay_id=? AND status='pending'", (replay["replay_id"],)).fetchone()[0]
        completed = connection.execute("SELECT count(*) FROM formal_approval_queue WHERE replay_id=? AND status!='pending'", (replay["replay_id"],)).fetchone()[0]
        reviews = connection.execute("SELECT count(*) FROM review_decision WHERE candidate_id IN (SELECT candidate_id FROM formal_approval_queue WHERE replay_id=?)", (replay["replay_id"],)).fetchone()[0]
        publications = connection.execute("SELECT count(*) FROM published_description WHERE candidate_id IN (SELECT candidate_id FROM formal_approval_queue WHERE replay_id=?)", (replay["replay_id"],)).fetchone()[0]
        print(json.dumps({
            "queued": inserted,
            "already_pending": already_pending,
            "pending": pending,
            "completed": completed,
            "formal_reviews_created": reviews,
            "published": publications,
            "by_question_mark_class": dict(sorted(class_counts.items())),
            "by_site": dict(sorted(site_counts.items())),
            "rule_key": RULE_KEY,
            "replay_id": replay["replay_id"],
            "source_write": False,
            "formal_publication": False,
        }, ensure_ascii=False))
    finally:
        connection.close()


if __name__ == "__main__":
    main()
