"""Backfill space-rule provenance for the already published local result batch."""
from __future__ import annotations

import csv
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parent
DB = ROOT / "data" / "semantic_workflow.sqlite3"
PREVIEW_DIR = PROJECT_ROOT / "pilots" / "HD_SAAS" / "space_rule_preview"
PREVIEW_CSV = PREVIEW_DIR / "formal_space_rewrite_preview.csv"
PREVIEW_MANIFEST = PREVIEW_DIR / "formal_preview_manifest.json"
OUTPUT_MANIFEST = PREVIEW_DIR / "provenance_backfill_manifest.json"
RULE_VERSION = "space-normalization-proposed-20260812-v1"
ACTOR = "local-space-provenance-repair"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def main() -> None:
    manifest = json.loads(PREVIEW_MANIFEST.read_text(encoding="utf-8"))
    rows = list(csv.DictReader(PREVIEW_CSV.open(encoding="utf-8-sig", newline="")))
    if len(rows) != 776 or manifest.get("preview_rows") != 776:
        raise SystemExit("Space preview must contain exactly 776 rows.")

    preview_by_id = {row["CANDIDATE_ID"]: row for row in rows}
    if len(preview_by_id) != len(rows):
        raise SystemExit("Space preview contains duplicate candidate ids.")

    connection = sqlite3.connect(DB, timeout=60)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=60000")
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        published_rows = connection.execute(
            """
            SELECT p.candidate_id,p.publication_id,p.final_description,
              c.applied_rule_ids_json,c.publication_state,c.review_state,
              p.rule_version
            FROM published_description p
            JOIN semantic_candidate c ON c.candidate_id=p.candidate_id
            WHERE p.publication_id LIKE 'publication-space-%'
            ORDER BY p.candidate_id
            """
        ).fetchall()
        if len(published_rows) != 776:
            raise SystemExit(f"Expected 776 space publications, found {len(published_rows)}.")

        now = utc_now()
        changed = 0
        unchanged = 0
        connection.execute("BEGIN IMMEDIATE")
        for published in published_rows:
            candidate_id = published["candidate_id"]
            preview = preview_by_id.get(candidate_id)
            if preview is None:
                connection.rollback()
                raise SystemExit(f"Published candidate is absent from preview: {candidate_id}")
            if published["rule_version"] != RULE_VERSION:
                connection.rollback()
                raise SystemExit(f"Unexpected rule version for {candidate_id}")
            if published["final_description"] != preview["PROPOSED_DESCRIPTION"]:
                connection.rollback()
                raise SystemExit(f"Published description differs from preview: {candidate_id}")
            if published["publication_state"] != "published" or published["review_state"] != "approved":
                connection.rollback()
                raise SystemExit(f"Publication state is not final for {candidate_id}")

            existing = json.loads(published["applied_rule_ids_json"] or "[]")
            if not isinstance(existing, list):
                connection.rollback()
                raise SystemExit(f"Invalid rule provenance JSON: {candidate_id}")
            expected = [key for key in preview["APPLIED_RULE_KEYS"].split("|") if key]
            merged = list(dict.fromkeys([*existing, *expected]))
            if merged == existing:
                unchanged += 1
                continue
            connection.execute(
                "UPDATE semantic_candidate SET applied_rule_ids_json=? WHERE candidate_id=?",
                (json.dumps(merged, ensure_ascii=False), candidate_id),
            )
            changed += 1

        payload = {
            "preview_id": manifest["preview_id"],
            "preview_sha256": manifest["preview_sha256"],
            "rule_version": RULE_VERSION,
            "candidate_count": len(published_rows),
            "changed_candidates": changed,
            "unchanged_candidates": unchanged,
            "source_write": False,
            "formal_result_layer_only": True,
            "updated_at_utc": now,
        }
        connection.execute(
            "INSERT INTO audit_event(entity_type,entity_id,event_type,actor,payload_json,event_at) VALUES (?,?,?,?,?,?)",
            ("publication", manifest["preview_id"], "space_rule_provenance_backfilled", ACTOR, json.dumps(payload, ensure_ascii=False), now),
        )
        connection.commit()
        OUTPUT_MANIFEST.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    finally:
        connection.close()


if __name__ == "__main__":
    main()
