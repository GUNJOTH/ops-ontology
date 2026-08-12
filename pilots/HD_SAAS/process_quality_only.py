"""Process only context-complete HD quality candidates into an approval-ready set."""
from __future__ import annotations

import csv
import hashlib
import json
import pathlib
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parent
INPUT = ROOT / "quality" / "hd_quality_candidates.csv"
OUTPUT_DIR = ROOT / "processed_quality"
OUTPUT = OUTPUT_DIR / "hd_processed_quality_candidates.csv"
MANIFEST = OUTPUT_DIR / "manifest.json"


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    process_run_id = "hd-quality-process-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    processed_at = datetime.now(timezone.utc).isoformat()
    processed_rows = 0
    fields: list[str] | None = None
    with INPUT.open(encoding="utf-8-sig", newline="") as input_handle:
        reader = csv.DictReader(input_handle)
        if not reader.fieldnames:
            raise SystemExit("质量结果缺少表头")
        fields = list(reader.fieldnames) + [
            "UNIFIED_DESCRIPTION", "DESCRIPTION_PROCESS_RULE", "CONTEXT_VALIDATED", "EVIDENCE_COMPLETE",
            "PROCESS_RUN_ID", "PROCESSED_AT_UTC", "PROCESS_STATUS", "APPROVAL_STATUS", "PUBLISH_STATUS",
        ]
        with OUTPUT.open("w", encoding="utf-8-sig", newline="") as output_handle:
            writer = csv.DictWriter(output_handle, fieldnames=fields)
            writer.writeheader()
            for row in reader:
                if row.get("CONTEXT_STATUS") != "complete" or row.get("CONTEXT_REASON_CODES"):
                    raise SystemExit("质量输入包含上下文告警记录，已停止处理")
                row["UNIFIED_DESCRIPTION"] = row.get("CANDIDATE_DESCRIPTION", "")
                row["DESCRIPTION_PROCESS_RULE"] = "PRESERVE_NORMALIZED_SOURCE_DESCRIPTION_WITH_VALIDATED_CONTEXT"
                row["CONTEXT_VALIDATED"] = "1"
                row["EVIDENCE_COMPLETE"] = "1"
                row["PROCESS_RUN_ID"] = process_run_id
                row["PROCESSED_AT_UTC"] = processed_at
                row["PROCESS_STATUS"] = "ready_for_approval"
                row["APPROVAL_STATUS"] = "pending"
                row["PUBLISH_STATUS"] = "not_published"
                writer.writerow(row)
                processed_rows += 1
    source_snapshot_id = json.loads((ROOT / "snapshot" / "manifest.json").read_text(encoding="utf-8"))["source_snapshot_id"]
    manifest = {
        "process_run_id": process_run_id,
        "source_snapshot_id": source_snapshot_id,
        "input_quality_rows": processed_rows,
        "processed_rows": processed_rows,
        "processing_policy": "quality-only; context complete; preserve normalized source description until approval",
        "output": str(OUTPUT),
        "output_sha256": hashlib.sha256(OUTPUT.read_bytes()).hexdigest(),
        "source_write": False,
        "formal_publication": False,
        "approval_status": "pending",
    }
    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(manifest)


if __name__ == "__main__":
    main()
