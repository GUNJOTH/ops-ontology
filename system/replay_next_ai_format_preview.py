"""Replay the AI-confirmed 66-row rewrite and 65-row preserve previews."""
from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
import uuid
from pathlib import Path

from common import utc_now

ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parent
DB = ROOT / "data" / "semantic_workflow.sqlite3"
PREVIEW_DIR = PROJECT_ROOT / "pilots" / "HD_SAAS" / "next_ai_format_preview"
PREVIEW_CSV = PREVIEW_DIR / "safe_punctuation_formal_preview.csv"
PRESERVE_CSV = PREVIEW_DIR / "signed_value_preserve_result.csv"
MANIFEST_JSON = PREVIEW_DIR / "manifest.json"
REPLAY_JSON = PREVIEW_DIR / "replay_manifest.json"
RULE_VERSION = "ai-confirmed-format-preview-20260812-v1"
SAFE_MAPPINGS = {"\ufe51": "\u3001", "\uff1b": ";", "\uff1c": "<", "\uff1e": ">"}



def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_transform(value: str) -> str:
    return "".join(SAFE_MAPPINGS.get(char, char) for char in value)


def main() -> None:
    manifest = json.loads(MANIFEST_JSON.read_text(encoding="utf-8"))
    if manifest.get("rule_version") != RULE_VERSION or manifest.get("source_write") is not False or manifest.get("formal_publication") is not False:
        raise SystemExit("Preview manifest is not a read-only replay candidate.")
    if sha256(PREVIEW_CSV) != manifest.get("preview_sha256") or sha256(PRESERVE_CSV) != manifest.get("preserve_sha256"):
        raise SystemExit("Preview hash does not match manifest.")
    with PREVIEW_CSV.open(encoding="utf-8-sig", newline="") as handle:
        preview_rows = list(csv.DictReader(handle))
    with PRESERVE_CSV.open(encoding="utf-8-sig", newline="") as handle:
        preserve_rows = list(csv.DictReader(handle))
    if len(preview_rows) != 66 or len(preserve_rows) != 65:
        raise SystemExit(f"Preview count mismatch: {len(preview_rows)}/{len(preserve_rows)}")

    connection = sqlite3.connect(str(DB))
    connection.row_factory = sqlite3.Row
    ids = [row["CANDIDATE_ID"] for row in preview_rows + preserve_rows]
    marks = ",".join("?" for _ in ids)
    db_rows = connection.execute(
        f"""SELECT c.candidate_id,c.original_description,c.candidate_description,c.review_state,c.publication_state,
        c.validator_status,c.confidence,d.site_id,d.asset_number
        FROM semantic_candidate c JOIN device_identity d ON d.device_id=c.device_id
        WHERE c.candidate_id IN ({marks})""", ids,
    ).fetchall()
    if len(db_rows) != 131:
        raise SystemExit(f"Database/preview identity mismatch: {len(db_rows)}/131")
    db_map = {row["candidate_id"]: row for row in db_rows}
    pass_count = 0
    failures: list[str] = []
    for row in preview_rows:
        db_row = db_map[row["CANDIDATE_ID"]]
        ok = (
            db_row["review_state"] == "pending"
            and db_row["publication_state"] == "unpublished"
            and db_row["validator_status"] == "candidate"
            and db_row["confidence"] == "high"
            and row["ORIGINAL_DESCRIPTION"] == db_row["original_description"]
            and row["PROPOSED_DESCRIPTION"] == db_row["candidate_description"]
            and safe_transform(row["ORIGINAL_DESCRIPTION"]) == row["PROPOSED_DESCRIPTION"]
        )
        if ok:
            pass_count += 1
        else:
            failures.append(row["CANDIDATE_ID"])
    for row in preserve_rows:
        db_row = db_map[row["CANDIDATE_ID"]]
        ok = (
            db_row["review_state"] == "pending"
            and db_row["publication_state"] == "unpublished"
            and db_row["validator_status"] == "candidate"
            and db_row["confidence"] == "high"
            and row["ORIGINAL_DESCRIPTION"] == db_row["original_description"]
            and row["PROPOSED_DESCRIPTION"] == row["ORIGINAL_DESCRIPTION"]
            and "-" in row["ORIGINAL_DESCRIPTION"]
            and "-" not in db_row["candidate_description"]
        )
        if ok:
            pass_count += 1
        else:
            failures.append(row["CANDIDATE_ID"])
    if failures:
        raise SystemExit(f"Replay failed for {len(failures)} rows: {failures[:5]}")

    replay_id = f"replay-ai-format-{uuid.uuid4().hex}"
    now = utc_now()
    connection.execute(
        "INSERT INTO replay_run(replay_id,rule_version,validator_version,evaluation_count,pass_count,fail_count,status,started_at,finished_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (replay_id, RULE_VERSION, "hd-semantic-validator-0.2.0", 131, 131, 0, "passed", now, now),
    )
    connection.execute(
        "INSERT INTO audit_event(entity_type,entity_id,event_type,actor,payload_json,event_at) VALUES (?,?,?,?,?,?)",
        ("replay", replay_id, "ai_format_preview_replayed", "replay_next_ai_format_preview.py", json.dumps({"preview_rows": 66, "preserve_rows": 65, "pass_count": 131, "fail_count": 0, "source_write": False, "formal_publication": False}, ensure_ascii=False), now),
    )
    connection.commit()
    connection.close()
    replay = {
        "replay_id": replay_id,
        "rule_version": RULE_VERSION,
        "evaluation_count": 131,
        "pass_count": pass_count,
        "fail_count": 0,
        "status": "passed",
        "preview_rows": 66,
        "preserve_rows": 65,
        "source_write": False,
        "formal_publication": False,
        "next_gate": "user confirmation before any publication",
    }
    REPLAY_JSON.write_text(json.dumps(replay, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(replay, ensure_ascii=False))


if __name__ == "__main__":
    main()
