"""Generate a read-only preview for approved but not yet published candidates."""
from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DB = ROOT / "data" / "semantic_workflow.sqlite3"
OUT = ROOT.parent / "pilots" / "HD_SAAS" / "approved_unpublished_preview"
PREVIEW = OUT / "publication_preview.csv"
SAMPLE = OUT / "sample_200.csv"
MANIFEST = OUT / "manifest.json"


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def stratified_sample(rows: list[dict[str, str]], target: int) -> list[dict[str, str]]:
    groups: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        groups[row["SITE_ID"]].append(row)
    selected: list[dict[str, str]] = []
    keys = sorted(groups)
    cursor = 0
    while len(selected) < min(target, len(rows)):
        key = keys[cursor % len(keys)]
        if groups[key]:
            selected.append(groups[key].pop(0))
        cursor += 1
    return selected


def write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    fields = list(rows[0]) if rows else ["CANDIDATE_ID"]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    rows = connection.execute(
        """
        SELECT c.candidate_id,c.batch_id,c.original_description,c.candidate_description,
          c.rule_version,c.validator_version,c.review_state,c.publication_state,
          d.source_snapshot_id,d.source_schema,d.source_asset_id,d.site_id,d.asset_number,
          d.location_code,d.location_description,d.location_parent,d.classification_description,
          r.review_id,r.decision,r.reviewed_description,r.reason_code,r.reviewer,
          r.approval_receipt,r.reviewed_at
        FROM semantic_candidate c
        JOIN device_identity d ON d.device_id=c.device_id
        JOIN review_decision r ON r.candidate_id=c.candidate_id
        WHERE c.review_state='approved' AND c.publication_state='unpublished'
          AND r.decision IN ('approved','modified')
          AND trim(COALESCE(r.reviewed_description,'')) <> ''
          AND NOT EXISTS (
            SELECT 1 FROM published_description p
            WHERE p.source_schema=d.source_schema AND p.site_id=d.site_id AND p.asset_number=d.asset_number
          )
        ORDER BY d.site_id,d.asset_number,c.candidate_id
        """
    ).fetchall()
    connection.close()

    output: list[dict[str, str]] = []
    for row in rows:
        item = dict(row)
        item.update(
            {
                "FINAL_DESCRIPTION": item.pop("reviewed_description") or "",
                "SOURCE_WRITE": "false",
                "FORMAL_PUBLICATION": "false",
            }
        )
        output.append({key.upper(): "" if value is None else str(value) for key, value in item.items()})

    if not output:
        raise SystemExit("No approved unpublished candidates are eligible for preview.")
    write_csv(PREVIEW, output)
    sample = stratified_sample(output, 200)
    write_csv(SAMPLE, sample)

    by_site: dict[str, int] = defaultdict(int)
    by_reason: dict[str, int] = defaultdict(int)
    for row in output:
        by_site[row["SITE_ID"]] += 1
        by_reason[row["REASON_CODE"]] += 1
    manifest = {
        "preview_id": f"approved-unpublished-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}",
        "generated_at": now(),
        "source_scope": "semantic_candidate approved + unpublished + review_decision approved/modified",
        "preview_rows": len(output),
        "sample_rows": len(sample),
        "sites": dict(sorted(by_site.items())),
        "reason_codes": dict(sorted(by_reason.items())),
        "preview_sha256": digest(PREVIEW),
        "sample_sha256": digest(SAMPLE),
        "source_write": False,
        "formal_publication": False,
        "publication_status": "preview_only",
    }
    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False))


if __name__ == "__main__":
    main()
