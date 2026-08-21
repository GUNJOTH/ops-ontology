"""Create a read-only baseline for the completed formal semantic release."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DB = ROOT / "data" / "semantic_workflow.sqlite3"
BASELINE_ROOT = ROOT.parent / "pilots" / "HD_SAAS" / "release_baselines"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    generated_at = datetime.now(timezone.utc)
    baseline_id = f"formal-semantic-baseline-{generated_at.strftime('%Y%m%dT%H%M%SZ')}"
    output_dir = BASELINE_ROOT / baseline_id
    output_dir.mkdir(parents=True, exist_ok=False)

    connection = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        batch = connection.execute("SELECT * FROM batch_run ORDER BY started_at DESC LIMIT 1").fetchone()
        if batch is None:
            raise SystemExit("No semantic batch exists.")
        snapshot = connection.execute(
            "SELECT * FROM source_snapshot WHERE source_snapshot_id=?",
            (batch["source_snapshot_id"],),
        ).fetchone()
        if snapshot is None:
            raise SystemExit("The latest batch source snapshot is missing.")

        counts = {
            "input_count": int(batch["input_count"]),
            "candidate_count": int(connection.execute("SELECT count(*) FROM semantic_candidate WHERE batch_id=?", (batch["batch_id"],)).fetchone()[0]),
            "approved_count": int(connection.execute("SELECT count(*) FROM semantic_candidate WHERE batch_id=? AND review_state IN ('approved','modified')", (batch["batch_id"],)).fetchone()[0]),
            "published_count": int(connection.execute("SELECT count(*) FROM published_description").fetchone()[0]),
            "pending_count": int(connection.execute("SELECT count(*) FROM semantic_candidate WHERE batch_id=? AND review_state='pending'", (batch["batch_id"],)).fetchone()[0]),
            "rejected_count": int(connection.execute("SELECT count(*) FROM semantic_candidate WHERE batch_id=? AND review_state='rejected'", (batch["batch_id"],)).fetchone()[0]),
            "published_identity_duplicates": int(connection.execute("""
                SELECT count(*) FROM (
                  SELECT source_schema,site_id,asset_number,count(*) AS n
                  FROM published_description
                  GROUP BY source_schema,site_id,asset_number
                  HAVING count(*) > 1
                )
            """).fetchone()[0]),
            "unapproved_publications": int(connection.execute("""
                SELECT count(*)
                FROM published_description p
                LEFT JOIN review_decision r ON r.candidate_id=p.candidate_id
                WHERE r.review_id IS NULL OR r.decision NOT IN ('approved','modified')
            """).fetchone()[0]),
        }
        published_by_site = {
            row["site_id"]: int(row["count"])
            for row in connection.execute(
                "SELECT site_id,count(*) AS count FROM published_description GROUP BY site_id ORDER BY site_id"
            ).fetchall()
        }
        active_rules = [
            dict(row)
            for row in connection.execute(
                "SELECT rule_key,cleaning_type,rule_version,replay_id,enabled,updated_at FROM cleaning_rule_registry WHERE enabled=1 ORDER BY rule_key"
            ).fetchall()
        ]
        latest_replay = connection.execute(
            "SELECT replay_id,rule_version,validator_version,evaluation_count,pass_count,fail_count,status,finished_at FROM replay_run ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        latest_replay_payload = dict(latest_replay) if latest_replay else None
    finally:
        connection.close()

    export_candidates = sorted(
        (ROOT.parent / "pilots" / "HD_SAAS" / "approved_unpublished_preview").glob("published_export_*.csv"),
        key=lambda path: path.stat().st_mtime,
    )
    export = export_candidates[-1] if export_candidates else None
    payload = {
        "baseline_id": baseline_id,
        "generated_at": generated_at.isoformat(),
        "status": "complete" if counts["published_identity_duplicates"] == 0 and counts["unapproved_publications"] == 0 and counts["pending_count"] == 0 else "blocked",
        "batch": {
            "batch_id": batch["batch_id"],
            "source_snapshot_id": batch["source_snapshot_id"],
            "rule_version": batch["rule_version"],
            "validator_version": batch["validator_version"],
            "started_at": batch["started_at"],
            "finished_at": batch["finished_at"],
        },
        "source_snapshot": {
            "source_snapshot_id": snapshot["source_snapshot_id"],
            "source_schema": snapshot["source_table"],
            "source_row_count": snapshot["source_row_count"],
            "distinct_identity_count": snapshot["distinct_identity_count"],
            "snapshot_hash": snapshot["snapshot_hash"],
            "source_write": bool(snapshot["source_write"]),
        },
        "counts": counts,
        "published_by_site": published_by_site,
        "active_rules": active_rules,
        "latest_replay": latest_replay_payload,
        "formal_export": {
            "path": str(export) if export else None,
            "sha256": sha256(export) if export else None,
        },
        "source_write": False,
        "formal_publication": True,
        "next_action": "capture a new read-only source snapshot and process only added or changed identities",
    }
    manifest = output_dir / "manifest.json"
    manifest.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"baseline_id": baseline_id, "manifest": str(manifest), "status": payload["status"], "counts": counts}, ensure_ascii=False))


if __name__ == "__main__":
    main()
