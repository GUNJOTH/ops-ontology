"""Export only context-complete HD_SAAS candidates as the quality set."""
from __future__ import annotations

import csv
import hashlib
import json
import pathlib
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parent
INPUT = ROOT / "context_candidates" / "equipment_description_context_candidates.csv"
QUALITY_DIR = ROOT / "quality"
OUTPUT = QUALITY_DIR / "hd_quality_candidates.csv"
MANIFEST = QUALITY_DIR / "manifest.json"


def main() -> None:
    QUALITY_DIR.mkdir(parents=True, exist_ok=True)
    quality_rows = 0
    review_rows = 0
    fields: list[str] | None = None
    with INPUT.open(encoding="utf-8-sig", newline="") as input_handle:
        reader = csv.DictReader(input_handle)
        if not reader.fieldnames:
            raise SystemExit("上下文候选缺少表头")
        fields = list(reader.fieldnames)
        with OUTPUT.open("w", encoding="utf-8-sig", newline="") as output_handle:
            writer = csv.DictWriter(output_handle, fieldnames=fields)
            writer.writeheader()
            for row in reader:
                if row.get("CONTEXT_STATUS") == "complete" and not row.get("CONTEXT_REASON_CODES"):
                    writer.writerow(row)
                    quality_rows += 1
                else:
                    review_rows += 1
    snapshot_id = json.loads((ROOT / "snapshot" / "manifest.json").read_text(encoding="utf-8"))["source_snapshot_id"]
    output_hash = hashlib.sha256(OUTPUT.read_bytes()).hexdigest()
    manifest = {
        "quality_run_id": "hd-quality-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
        "source_snapshot_id": snapshot_id,
        "input_context_candidates": quality_rows + review_rows,
        "quality_rows": quality_rows,
        "held_for_context_review": review_rows,
        "quality_rule": "CONTEXT_STATUS=complete and CONTEXT_REASON_CODES empty",
        "output": str(OUTPUT),
        "output_sha256": output_hash,
        "formal_publication": False,
        "source_write": False,
    }
    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(manifest)


if __name__ == "__main__":
    main()
