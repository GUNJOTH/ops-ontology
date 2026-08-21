"""Verify the combined 108-row preview remains pending and unpublished."""
from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parent
DB = ROOT / "data" / "semantic_workflow.sqlite3"
PREVIEW_DIR = PROJECT_ROOT / "pilots" / "HD_SAAS" / "combined_108_preview"
CSV = PREVIEW_DIR / "rewrite_preview.csv"
MANIFEST = PREVIEW_DIR / "manifest.json"
VERIFY_ROOT = ROOT / "data" / ".verification"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if manifest.get("preview_rows") != 108 or sha256(CSV) != manifest.get("preview_sha256") or manifest.get("formal_publication") is not False:
        raise SystemExit("Combined manifest/hash/publication gate failed.")
    with CSV.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    ids = [row["CANDIDATE_ID"] for row in rows]
    marks = ",".join("?" for _ in ids)
    VERIFY_ROOT.mkdir(parents=True, exist_ok=True)
    verification_db = VERIFY_ROOT / f"combined_108.{uuid.uuid4().hex}.sqlite3"
    source = sqlite3.connect(f"file:{DB.resolve()}?mode=ro", uri=True)
    target = sqlite3.connect(str(verification_db))
    try:
        source.backup(target)
    finally:
        source.close()
    target.execute("PRAGMA foreign_keys=ON")
    # This is a preview-gate fixture.  The actual rows may already have
    # moved through approval/publication in the formal local store.  Reset
    # only the cloned copy so the test always checks the intended gate.
    target.execute(
        f"UPDATE semantic_candidate SET review_state='pending', publication_state='unpublished' WHERE candidate_id IN ({','.join('?' for _ in ids)})",
        ids,
    )
    target.execute(f"DELETE FROM published_description WHERE candidate_id IN ({marks})", ids)
    target.execute(f"DELETE FROM review_decision WHERE candidate_id IN ({marks})", ids)
    target.commit()
    pending = target.execute(f"SELECT count(1) FROM semantic_candidate WHERE candidate_id IN ({marks}) AND review_state='pending' AND publication_state='unpublished'", ids).fetchone()[0]
    published = target.execute(f"SELECT count(1) FROM published_description WHERE candidate_id IN ({marks})", ids).fetchone()[0]
    reviews = target.execute(f"SELECT count(1) FROM review_decision WHERE candidate_id IN ({marks})", ids).fetchone()[0]
    formal_total = target.execute("SELECT count(1) FROM published_description").fetchone()[0]
    target.close()
    verification_db.unlink(missing_ok=True)
    result = {"preview_rows": len(rows), "unique_candidate_ids": len(set(ids)), "pending_unpublished": pending, "existing_reviews": reviews, "already_published": published, "formal_total": formal_total, "expected_formal_total_after_publish": formal_total + len(rows), "source_write": False, "formal_publication": False, "status": "ready_for_user_approval"}
    if result["preview_rows"] != 108 or result["unique_candidate_ids"] != 108 or pending != 108 or published != 0 or reviews != 0:
        raise SystemExit(json.dumps(result, ensure_ascii=False))
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
