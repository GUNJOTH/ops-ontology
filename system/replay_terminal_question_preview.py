"""Replay terminal-question removal and assert internal question marks are untouched."""
from __future__ import annotations

import csv
import json
import sqlite3
import uuid
from pathlib import Path

from common import DEFAULT_WORKFLOW, utc_now
from common import sha256_file as sha256
from pipeline.contracts import connect_local

ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parent
DB = DEFAULT_WORKFLOW
PREVIEW_DIR = PROJECT_ROOT / "pilots" / "HD_SAAS" / "terminal_question_preview"
PREVIEW_CSV = PREVIEW_DIR / "rewrite_preview.csv"
MANIFEST_JSON = PREVIEW_DIR / "manifest.json"
REPLAY_JSON = PREVIEW_DIR / "replay_manifest.json"


def main() -> None:
    manifest = json.loads(MANIFEST_JSON.read_text(encoding="utf-8"))
    if manifest.get("preview_rows") != 42 or manifest.get("formal_publication") is not False or sha256(PREVIEW_CSV) != manifest.get("preview_sha256"):
        raise SystemExit("Terminal question preview manifest/hash gate failed.")
    with PREVIEW_CSV.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    connection = connect_local(str(DB))
    connection.row_factory = sqlite3.Row
    ids = [row["CANDIDATE_ID"] for row in rows]
    marks = ",".join("?" for _ in ids)
    db_rows = connection.execute(
        f"""SELECT c.candidate_id,c.original_description,c.candidate_description,c.review_state,c.publication_state,c.validator_status,c.confidence
        FROM semantic_candidate c WHERE c.candidate_id IN ({marks})""", ids,
    ).fetchall()
    if len(db_rows) != 42:
        raise SystemExit(f"Preview/database mismatch: {len(db_rows)}")
    pass_count = 0
    for row in rows:
        db = next(item for item in db_rows if item["candidate_id"] == row["CANDIDATE_ID"])
        expected = db["candidate_description"][:-1].rstrip()
        if (
            db["review_state"] == "pending" and db["publication_state"] == "unpublished"
            and db["validator_status"] == "candidate" and db["confidence"] == "high"
            and row["EXISTING_CANDIDATE_DESCRIPTION"] == db["candidate_description"]
            and row["PROPOSED_DESCRIPTION"] == expected
            and "?" not in row["PROPOSED_DESCRIPTION"]
            and db["candidate_description"].endswith("?")
        ):
            pass_count += 1
    internal_rows = connection.execute(
        """SELECT count(1) FROM semantic_candidate
        WHERE publication_state='unpublished' AND review_state='pending'
          AND validator_status='candidate' AND confidence='high'
          AND instr(candidate_description,'?')>0
          AND NOT candidate_description LIKE '%' || '?'"""
    ).fetchone()[0]
    if pass_count != 42 or internal_rows < 477:
        raise SystemExit(f"Replay failed: terminal={pass_count}, internal_remaining={internal_rows}")
    replay_id = f"replay-terminal-question-{uuid.uuid4().hex}"
    now = utc_now()
    connection.execute(
        "INSERT INTO replay_run(replay_id,rule_version,validator_version,evaluation_count,pass_count,fail_count,status,started_at,finished_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (replay_id, manifest["rule_version"], "hd-semantic-validator-0.2.0", 42, 42, 0, "passed", now, now),
    )
    connection.execute(
        "INSERT INTO audit_event(entity_type,entity_id,event_type,actor,payload_json,event_at) VALUES (?,?,?,?,?,?)",
        ("replay", replay_id, "terminal_question_preview_replayed", "replay_terminal_question_preview.py", json.dumps({"evaluation_count": 42, "pass_count": 42, "internal_question_marks_untouched": internal_rows, "source_write": False, "formal_publication": False}, ensure_ascii=False), now),
    )
    connection.commit()
    connection.close()
    result = {"replay_id": replay_id, "evaluation_count": 42, "pass_count": 42, "fail_count": 0, "status": "passed", "internal_question_marks_untouched": internal_rows, "source_write": False, "formal_publication": False, "next_gate": "confirm terminal-only rule before widening publication"}
    REPLAY_JSON.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()