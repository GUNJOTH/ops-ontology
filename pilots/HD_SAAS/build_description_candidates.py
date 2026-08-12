"""Build source-grounded HD_SAAS equipment description candidates."""
from __future__ import annotations

import csv
import hashlib
import json
import pathlib
import re
import unicodedata
from collections import Counter
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parent
SNAPSHOT_DIR = ROOT / "snapshot"
OUTPUT_DIR = ROOT / "candidates"
RULE_VERSION = "1.0.5"
VALIDATOR_VERSION = "hd-description-validator-1.0.0"


def normalize(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = re.sub(r"[\t\r\n]+", " ", text)
    return re.sub(r"\s+", " ", text).strip().strip(" ,，;；:：、·.。-—")


def is_stopped(value: str) -> bool:
    return normalize(value) in {"停止使用", "停用", "报废", "注销"}


def build_candidate(row: dict[str, str], source_snapshot_id: str) -> dict[str, str]:
    site = normalize(row.get("SITEID"))
    asset_number = normalize(row.get("ASSETNUM"))
    asset_id = normalize(row.get("ASSETID"))
    raw_description = row.get("DESCRIPTION") or ""
    normalized_description = normalize(raw_description)
    source_row_hash = hashlib.sha256(
        json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    candidate_id = hashlib.sha256(
        f"HD_SAAS|{site}|{asset_number}|{RULE_VERSION}".encode("utf-8")
    ).hexdigest()
    reasons: list[str] = []
    status = "candidate"
    candidate = normalized_description

    if not site or not asset_number:
        status = "blocked"
        reasons.append("MISSING_STABLE_IDENTITY")
    elif not normalized_description:
        status = "needs_review"
        candidate = ""
        reasons.append("EMPTY_DESCRIPTION")
    elif normalized_description == asset_number or normalized_description.lower() in {"string", "none", "null", "设备", "未知"}:
        status = "needs_review"
        reasons.append("PLACEHOLDER_OR_CODE_DESCRIPTION")
    elif len(normalized_description) < 2:
        status = "needs_review"
        reasons.append("INSUFFICIENT_DESCRIPTION")

    if is_stopped(row.get("STATUS", "")):
        status = "needs_review" if status == "candidate" else status
        reasons.append("STOPPED_DEVICE")

    evidence = {
        "DESCRIPTION": raw_description,
        "STATUS": row.get("STATUS", ""),
        "LOCATION": row.get("LOCATION", ""),
        "PARENT": row.get("PARENT", ""),
        "CLASSSTRUCTUREID": row.get("CLASSSTRUCTUREID", ""),
        "MANUFACTURER": row.get("MANUFACTURER", ""),
        "PLUSCMODELNUM": row.get("PLUSCMODELNUM", ""),
        "S_MODELNUM": row.get("S_MODELNUM", ""),
        "ITEMNUM": row.get("ITEMNUM", ""),
    }
    return {
        "CANDIDATE_ID": candidate_id,
        "SOURCE_SNAPSHOT_ID": source_snapshot_id,
        "SOURCE_SCHEMA": "HD_SAAS",
        "SOURCE_TABLE": "ASSET",
        "SOURCE_ROW_HASH": source_row_hash,
        "ASSETID": asset_id,
        "SITEID": site,
        "ASSETNUM": asset_number,
        "STATUS": normalize(row.get("STATUS")),
        "LOCATION": normalize(row.get("LOCATION")),
        "CLASSSTRUCTUREID": normalize(row.get("CLASSSTRUCTUREID")),
        "ORIGINAL_DESCRIPTION": raw_description,
        "NORMALIZED_DESCRIPTION": normalized_description,
        "CANDIDATE_DESCRIPTION": candidate,
        "RESULT_STATUS": status,
        "REASON_CODES": ",".join(reasons),
        "EVIDENCE_JSON": json.dumps(evidence, ensure_ascii=False, separators=(",", ":")),
        "RULE_VERSION": RULE_VERSION,
        "VALIDATOR_VERSION": VALIDATOR_VERSION,
        "CREATED_AT_UTC": datetime.now(timezone.utc).isoformat(),
    }


def main() -> None:
    manifest_path = SNAPSHOT_DIR / "manifest.json"
    if not manifest_path.exists():
        raise SystemExit("缺少 snapshot/manifest.json，必须先完成只读源快照")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    output_path = OUTPUT_DIR / "equipment_description_candidates.csv"
    fields: list[str] | None = None
    counts: Counter[str] = Counter()
    rows = 0
    with output_path.open("w", encoding="utf-8-sig", newline="") as output_handle:
        writer = None
        for source_path in sorted(SNAPSHOT_DIR.glob("*.csv")):
            with source_path.open(encoding="utf-8-sig", newline="") as source_handle:
                for source_row in csv.DictReader(source_handle):
                    candidate = build_candidate(source_row, manifest["source_snapshot_id"])
                    if fields is None:
                        fields = list(candidate)
                        writer = csv.DictWriter(output_handle, fieldnames=fields)
                        writer.writeheader()
                    assert writer is not None
                    writer.writerow(candidate)
                    rows += 1
                    counts[candidate["RESULT_STATUS"]] += 1
    if fields is None:
        raise SystemExit("快照目录没有 CSV 数据")
    result_manifest = {
        "source_snapshot_id": manifest["source_snapshot_id"],
        "candidate_run_id": "hd-candidates-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
        "source_schema": "HD_SAAS",
        "source_table": "ASSET",
        "source_rows_processed": rows,
        "result_status_counts": dict(counts),
        "rule_version": RULE_VERSION,
        "validator_version": VALIDATOR_VERSION,
        "formal_publication": False,
        "output": str(output_path),
    }
    (OUTPUT_DIR / "manifest.json").write_text(json.dumps(result_manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(result_manifest)


if __name__ == "__main__":
    main()
