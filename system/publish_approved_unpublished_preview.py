"""Publish an approved-unpublished preview through an explicit local gate.

The command may write only the local formal result layer.  It never writes a
source system.  A PipelineContext run manifest is emitted alongside the
business replay/publication manifests so the high-risk operation is resumable
and auditable as one step.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path

from common import DEFAULT_WORKFLOW
from common import sha256_file as digest
from common import utc_now as now
from pipeline.contracts import connect_local
from pipeline.entrypoint import add_pipeline_arguments, run_single_step

ROOT = Path(__file__).resolve().parent
DB = DEFAULT_WORKFLOW
OUT = ROOT.parent / "pilots" / "HD_SAAS" / "approved_unpublished_preview"
PREVIEW = OUT / "publication_preview.csv"
REPLAY = OUT / "replay_manifest.json"
PUBLICATION = OUT / "publication_manifest.json"
BACKUPS = ROOT / "backups"
PUBLISHER = "local-user-approved-unpublished"


def publish_preview(
    db_path: Path = DB,
    preview_path: Path = PREVIEW,
    replay_path: Path = REPLAY,
    publication_path: Path = PUBLICATION,
    backup_dir: Path = BACKUPS,
) -> dict[str, object]:
    replay = json.loads(replay_path.read_text(encoding="utf-8"))
    if (
        replay.get("status") != "passed"
        or replay.get("evaluation_count") != replay.get("pass_count")
        or replay.get("fail_count") != 0
        or replay.get("source_write") is not False
        or replay.get("formal_publication") is not False
    ):
        raise RuntimeError("Passed read-only replay is required.")
    if replay.get("preview_sha256") != digest(preview_path):
        raise RuntimeError("Preview changed after replay.")
    with preview_path.open(encoding="utf-8-sig", newline="") as handle:
        preview = list(csv.DictReader(handle))
    if len(preview) != replay["evaluation_count"]:
        raise RuntimeError("Preview/replay count mismatch.")

    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = backup_dir / (
        "semantic_workflow_before_approved_unpublished_publish_"
        f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.sqlite3"
    )
    shutil.copy2(db_path, backup)
    connection = connect_local(db_path, timeout=60)
    try:
        connection.execute("PRAGMA busy_timeout=60000")
        existing_replay = connection.execute(
            "SELECT 1 FROM replay_run WHERE replay_id=?", (replay["replay_id"],)
        ).fetchone()
        if existing_replay is None:
            replay_time = now()
            connection.execute(
                "INSERT INTO replay_run(replay_id,rule_version,validator_version,evaluation_count,pass_count,fail_count,status,started_at,finished_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    replay["replay_id"],
                    "approved-unpublished-replay-v1",
                    "approved-unpublished-validator-v1",
                    replay["evaluation_count"],
                    replay["pass_count"],
                    replay["fail_count"],
                    "passed",
                    replay_time,
                    replay_time,
                ),
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
            raise RuntimeError("Candidate count changed after replay.")
        for item in preview:
            row = by_id[item["CANDIDATE_ID"]]
            if (
                row["review_state"] != "approved"
                or row["publication_state"] != "unpublished"
                or row["decision"] not in {"approved", "modified"}
                or not (row["reviewed_description"] or "").strip()
            ):
                raise RuntimeError(f"Approval state changed: {item['CANDIDATE_ID']}")
            if row["reviewed_description"] != item["FINAL_DESCRIPTION"]:
                raise RuntimeError(f"Final description changed: {item['CANDIDATE_ID']}")
            if connection.execute(
                "SELECT 1 FROM published_description WHERE source_schema=? AND site_id=? AND asset_number=?",
                (row["source_schema"], row["site_id"], row["asset_number"]),
            ).fetchone():
                raise RuntimeError(f"Published identity collision: {item['CANDIDATE_ID']}")

        published = 0
        run_id = f"approved-unpublished-publication-{uuid.uuid4().hex}"
        timestamp = now()
        connection.commit()
        connection.execute("BEGIN IMMEDIATE")
        for item in preview:
            row = by_id[item["CANDIDATE_ID"]]
            final = row["reviewed_description"]
            publication_hash = hashlib.sha256(
                json.dumps(
                    {
                        "source_schema": row["source_schema"],
                        "site_id": row["site_id"],
                        "asset_number": row["asset_number"],
                        "source_snapshot_id": row["source_snapshot_id"],
                        "final_description": final,
                        "rule_version": row["rule_version"],
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ).encode("utf-8")
            ).hexdigest()
            connection.execute(
                "INSERT INTO published_description(publication_id,candidate_id,review_id,source_snapshot_id,source_schema,site_id,asset_number,final_description,rule_version,validator_version,replay_id,published_by,published_at,publication_hash) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    f"publication-approved-{row['candidate_id']}",
                    row["candidate_id"],
                    row["review_id"],
                    row["source_snapshot_id"],
                    row["source_schema"],
                    row["site_id"],
                    row["asset_number"],
                    final,
                    row["rule_version"],
                    row["validator_version"],
                    replay["replay_id"],
                    PUBLISHER,
                    timestamp,
                    publication_hash,
                ),
            )
            connection.execute(
                "UPDATE semantic_candidate SET publication_state='published' WHERE candidate_id=?",
                (row["candidate_id"],),
            )
            published += 1
        for batch_id in sorted({row["batch_id"] for row in by_id.values()}):
            connection.execute(
                "UPDATE batch_run SET published_count=(SELECT count(*) FROM published_description p JOIN semantic_candidate c ON c.candidate_id=p.candidate_id WHERE c.batch_id=?) WHERE batch_id=?",
                (batch_id, batch_id),
            )
        payload: dict[str, object] = {
            "publication_run_id": run_id,
            "published_count": published,
            "preview_rows": len(preview),
            "replay_id": replay["replay_id"],
            "backup_path": str(backup),
            "published_at": timestamp,
            "source_write": False,
            "formal_publication": True,
            "status": "published",
        }
        connection.execute(
            "INSERT INTO audit_event(entity_type,entity_id,event_type,actor,payload_json,event_at) VALUES (?,?,?,?,?,?)",
            (
                "publication",
                run_id,
                "approved_unpublished_preview_published",
                PUBLISHER,
                json.dumps(payload, ensure_ascii=False),
                timestamp,
            ),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    publication_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Publish a replay-approved preview to the local formal result layer")
    parser.add_argument("--db", type=Path, default=DB)
    parser.add_argument("--preview", type=Path, default=PREVIEW)
    parser.add_argument("--replay", type=Path, default=REPLAY)
    parser.add_argument("--publication", type=Path, default=PUBLICATION)
    parser.add_argument("--backup-dir", type=Path, default=BACKUPS)
    parser.add_argument(
        "--confirm-formal-publication",
        action="store_true",
        help="Explicitly authorize writing the local formal result layer; source systems remain read-only",
    )
    add_pipeline_arguments(parser)
    args = parser.parse_args()
    if not args.confirm_formal_publication:
        print("未执行：需要 --confirm-formal-publication 才能写入本地正式结果层。")
        return 2
    try:
        pipeline = run_single_step(
            pipeline_id="approved-unpublished-publication",
            pipeline_version="approved-unpublished-publication-v1",
            step_id="approved_unpublished_publication",
            root=ROOT,
            parameters={
                "db": str(args.db.resolve()),
                "preview": str(args.preview.resolve()),
                "replay": str(args.replay.resolve()),
                "publicationManifest": str(args.publication.resolve()),
                "backupDir": str(args.backup_dir.resolve()),
                "explicitConfirmation": True,
            },
            handler=lambda _context, _dependencies: publish_preview(
                args.db.resolve(),
                args.preview.resolve(),
                args.replay.resolve(),
                args.publication.resolve(),
                args.backup_dir.resolve(),
            ),
            manifest_path=args.pipeline_manifest,
            resume_manifest_path=args.resume_manifest,
            allow_formal_publication=True,
        )
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(pipeline["outputs"]["approved_unpublished_publication"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
