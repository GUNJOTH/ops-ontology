"""Publish the approved-unpublished preview after a passed read-only replay."""
from __future__ import annotations

import csv
import hashlib
import json
import shutil
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DB = ROOT / "data" / "semantic_workflow.sqlite3"
OUT = ROOT.parent / "pilots" / "HD_SAAS" / "approved_unpublished_preview"
PREVIEW = OUT / "publication_preview.csv"
REPLAY = OUT / "replay_manifest.json"
PUBLICATION = OUT / "publication_manifest.json"
BACKUPS = ROOT / "backups"
PUBLISHER = "local-user-approved-unpublished"


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> None:
    replay = json.loads(REPLAY.read_text(encoding="utf-8"))
    if replay.get("status") != "passed" or replay.get("evaluation_count") != replay.get("pass_count") or replay.get("fail_count") != 0 or replay.get("source_write") is not False or replay.get("formal_publication") is not False:
        raise SystemExit("Passed read-only replay is required.")
    if replay.get("preview_sha256") != digest(PREVIEW):
        raise SystemExit("Preview changed after replay.")
    with PREVIEW.open(encoding="utf-8-sig", newline="") as handle:
        preview = list(csv.DictReader(handle))
    if len(preview) != replay["evaluation_count"]:
        raise SystemExit("Preview/replay count mismatch.")

    BACKUPS.mkdir(parents=True, exist_ok=True)
    backup = BACKUPS / f"semantic_workflow_before_approved_unpublished_publish_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.sqlite3"
    shutil.copy2(DB, backup)
    connection = sqlite3.connect(DB, timeout=60)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=60000")
    connection.execute("PRAGMA foreign_keys=ON")
    existing_replay = connection.execute("SELECT 1 FROM replay_run WHERE replay_id=?", (replay["replay_id"],)).fetchone()
    if existing_replay is None:
        replay_time = now()
        connection.execute(
            "INSERT INTO replay_run(replay_id,rule_version,validator_version,evaluation_count,pass_count,fail_count,status,started_at,finished_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (replay["replay_id"], "approved-unpublished-replay-v1", "approved-unpublished-validator-v1", replay["evaluation_count"], replay["pass_count"], replay["fail_count"], "passed", replay_time, replay_time),
        )
    ids = [row["CANDIDATE_ID"] for row in preview]
    rows = connection.execute(
        """
        SELECT c.candidate_id,c.batch_id,c.review_state,c.publication_state,c.validator_status,c.rule_version,c.validator_version,
          d.source_snapshot_id,d.source_schema,d.site_id,d.asset_number,
          r.review_id,r.reviewed_description,r.decision
        FROM semantic_candidate c JOIN device_identity d ON d.device_id=c.device_id JOIN review_decision r ON r.candidate_id=c.candidate_id
        WHERE c.review_state='approved'
          AND c.publication_state='unpublished'
          AND r.decision IN ('approved','modified')
          AND trim(COALESCE(r.reviewed_description,'')) <> ''
        """
    ).fetchall()
    by_id = {row["candidate_id"]: row for row in rows}
    if len(by_id) != len(ids):
        raise SystemExit("Candidate count changed after replay.")
    for item in preview:
        row = by_id[item["CANDIDATE_ID"]]
        if row["review_state"] != "approved" or row["publication_state"] != "unpublished" or row["decision"] not in {"approved", "modified"} or not (row["reviewed_description"] or "").strip():
            raise SystemExit(f"Approval state changed: {item['CANDIDATE_ID']}")
        if row["reviewed_description"] != item["FINAL_DESCRIPTION"]:
            raise SystemExit(f"Final description changed: {item['CANDIDATE_ID']}")
        if connection.execute("SELECT 1 FROM published_description WHERE source_schema=? AND site_id=? AND asset_number=?", (row["source_schema"], row["site_id"], row["asset_number"])).fetchone():
            raise SystemExit(f"Published identity collision: {item['CANDIDATE_ID']}")

    published = 0
    run_id = f"approved-unpublished-publication-{uuid.uuid4().hex}"
    timestamp = now()
    if connection.in_transaction:
        connection.commit()
    connection.execute("BEGIN IMMEDIATE")
    for item in preview:
        row = by_id[item["CANDIDATE_ID"]]
        final = row["reviewed_description"]
        publication_hash = hashlib.sha256(json.dumps({"source_schema": row["source_schema"], "site_id": row["site_id"], "asset_number": row["asset_number"], "source_snapshot_id": row["source_snapshot_id"], "final_description": final, "rule_version": row["rule_version"]}, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
        connection.execute(
            "INSERT INTO published_description(publication_id,candidate_id,review_id,source_snapshot_id,source_schema,site_id,asset_number,final_description,rule_version,validator_version,replay_id,published_by,published_at,publication_hash) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"publication-approved-{row['candidate_id']}", row["candidate_id"], row["review_id"], row["source_snapshot_id"], row["source_schema"], row["site_id"], row["asset_number"], final, row["rule_version"], row["validator_version"], replay["replay_id"], PUBLISHER, timestamp, publication_hash),
        )
        connection.execute("UPDATE semantic_candidate SET publication_state='published' WHERE candidate_id=?", (row["candidate_id"],))
        published += 1
    for batch_id in sorted({row["batch_id"] for row in by_id.values()}):
        connection.execute("UPDATE batch_run SET published_count=(SELECT count(*) FROM published_description p JOIN semantic_candidate c ON c.candidate_id=p.candidate_id WHERE c.batch_id=?) WHERE batch_id=?", (batch_id, batch_id))
    payload = {"publication_run_id": run_id, "published_count": published, "preview_rows": len(preview), "replay_id": replay["replay_id"], "backup_path": str(backup), "published_at": timestamp, "source_write": False, "formal_publication": True, "status": "published"}
    connection.execute("INSERT INTO audit_event(entity_type,entity_id,event_type,actor,payload_json,event_at) VALUES (?,?,?,?,?,?)", ("publication", run_id, "approved_unpublished_preview_published", PUBLISHER, json.dumps(payload, ensure_ascii=False), timestamp))
    connection.commit()
    PUBLICATION.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    connection.close()
    print(json.dumps(payload, ensure_ascii=False))


if __name__ == "__main__":
    main()
