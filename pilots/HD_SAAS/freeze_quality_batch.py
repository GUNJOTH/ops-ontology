"""Freeze the current HD quality batch by recording immutable file and source hashes."""
from __future__ import annotations

import csv
import hashlib
import json
import pathlib
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parent
QUALITY_FILE = ROOT / "quality" / "hd_quality_candidates.csv"
QUALITY_MANIFEST = ROOT / "quality" / "manifest.json"
FROZEN_DIR = ROOT / "frozen"
FROZEN_MANIFEST = FROZEN_DIR / "manifest.json"


def main() -> None:
    FROZEN_DIR.mkdir(parents=True, exist_ok=True)
    quality_manifest = json.loads(QUALITY_MANIFEST.read_text(encoding="utf-8"))
    with QUALITY_FILE.open(encoding="utf-8-sig", newline="") as handle:
        rows = sum(1 for _ in csv.DictReader(handle))
    file_hash = hashlib.sha256(QUALITY_FILE.read_bytes()).hexdigest()
    manifest = {
        "freeze_run_id": "hd-quality-freeze-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
        "frozen_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_snapshot_id": quality_manifest["source_snapshot_id"],
        "quality_file": str(QUALITY_FILE),
        "quality_file_sha256": file_hash,
        "quality_rows": rows,
        "quality_rule": "CONTEXT_STATUS=complete and CONTEXT_REASON_CODES empty",
        "rule_file": "../../rules/hd_quality_semantic_rules.yaml",
        "rule_version": "1.0.0",
        "source_write": False,
        "formal_publication": False,
        "freeze_policy": "input hash must match before any subsequent processing",
    }
    FROZEN_MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(manifest)


if __name__ == "__main__":
    main()
