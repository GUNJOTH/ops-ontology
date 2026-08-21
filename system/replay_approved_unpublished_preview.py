"""Replay an approved-but-unpublished preview without changing SQLite."""
from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

from common import utc_now
from pipeline.contracts import connect_readonly

ROOT = Path(__file__).resolve().parent
DB = ROOT / "data" / "semantic_workflow.sqlite3"
PREVIEW_DIR = ROOT.parent / "pilots" / "HD_SAAS" / "approved_unpublished_preview"
PREVIEW = PREVIEW_DIR / "publication_preview.csv"
REPLAY = PREVIEW_DIR / "replay_results.csv"
MANIFEST = PREVIEW_DIR / "replay_manifest.json"



def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    with PREVIEW.open(encoding="utf-8-sig", newline="") as handle:
        preview = list(csv.DictReader(handle))
    if not preview:
        raise SystemExit("Approved-unpublished preview is empty.")
    manifest_path = PREVIEW_DIR / "manifest.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        expected_rows = int(manifest.get("preview_rows") or 0)
        if expected_rows != len(preview):
            raise SystemExit(f"Preview/manifest count mismatch: {len(preview)} != {expected_rows}")

    connection = connect_readonly(DB)
    results: list[dict[str, str]] = []
    for item in preview:
        candidate = connection.execute(
            """
            SELECT c.candidate_id,c.original_description,c.candidate_description,c.review_state,c.publication_state,
              d.source_schema,d.site_id,d.asset_number,r.decision,r.reviewed_description,r.approval_receipt
            FROM semantic_candidate c
            JOIN device_identity d ON d.device_id=c.device_id
            JOIN review_decision r ON r.candidate_id=c.candidate_id
            WHERE c.candidate_id=?
            """,
            (item["CANDIDATE_ID"],),
        ).fetchone()
        failures: list[str] = []
        if candidate is None:
            failures.append("candidate_missing")
        else:
            if candidate["review_state"] != "approved":
                failures.append("review_state_changed")
            if candidate["publication_state"] != "unpublished":
                failures.append("publication_state_changed")
            if candidate["decision"] not in {"approved", "modified"}:
                failures.append("approval_decision_invalid")
            if not (candidate["approval_receipt"] or "").strip():
                failures.append("approval_receipt_missing")
            if candidate["original_description"] != item["ORIGINAL_DESCRIPTION"]:
                failures.append("original_description_changed")
            if candidate["candidate_description"] != item["CANDIDATE_DESCRIPTION"]:
                failures.append("candidate_description_changed")
            if (candidate["reviewed_description"] or "") != item["FINAL_DESCRIPTION"]:
                failures.append("final_description_changed")
            if (candidate["source_schema"], candidate["site_id"], candidate["asset_number"]) != (item["SOURCE_SCHEMA"], item["SITE_ID"], item["ASSET_NUMBER"]):
                failures.append("identity_changed")
            if connection.execute("SELECT 1 FROM published_description WHERE candidate_id=?", (item["CANDIDATE_ID"],)).fetchone() is not None:
                failures.append("already_published")
            if connection.execute("SELECT 1 FROM published_description WHERE source_schema=? AND site_id=? AND asset_number=?", (item["SOURCE_SCHEMA"], item["SITE_ID"], item["ASSET_NUMBER"])).fetchone() is not None:
                failures.append("identity_already_published")
        results.append({"CANDIDATE_ID": item["CANDIDATE_ID"], "SITE_ID": item["SITE_ID"], "ASSETNUM": item["ASSET_NUMBER"], "OUTCOME": "pass" if not failures else "fail", "FAILURES": json.dumps(failures, ensure_ascii=False)})
    connection.close()

    with REPLAY.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(results[0]))
        writer.writeheader()
        writer.writerows(results)
    passed = sum(row["OUTCOME"] == "pass" for row in results)
    failed = len(results) - passed
    payload = {"replay_id": f"approved-unpublished-replay-{hashlib.sha256(PREVIEW.read_bytes()).hexdigest()[:20]}", "generated_at": utc_now(), "preview_sha256": sha256(PREVIEW), "replay_sha256": sha256(REPLAY), "evaluation_count": len(results), "pass_count": passed, "fail_count": failed, "status": "passed" if failed == 0 else "failed", "source_write": False, "formal_publication": False}
    MANIFEST.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False))
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
