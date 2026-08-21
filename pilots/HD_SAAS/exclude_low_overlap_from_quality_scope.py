"""Derive a quality processing scope excluding LOW_TEXT_OVERLAP cases."""
from __future__ import annotations

import csv
import hashlib
import json
import pathlib
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parent
QUALITY = ROOT / "quality" / "hd_quality_candidates.csv"
LOW_OVERLAP = ROOT / "description_difference_analysis" / "description_difference_classification.csv"
OUTPUT_DIR = ROOT / "quality_scope"
FILTERED = OUTPUT_DIR / "hd_quality_candidates_for_unification.csv"
EXCLUDED = OUTPUT_DIR / "excluded_low_overlap_20260812.csv"
MANIFEST = OUTPUT_DIR / "manifest.json"
VERIFICATION = OUTPUT_DIR / "verification.json"
REPORT = ROOT / "reports" / "quality_scope_after_low_overlap_exclusion_20260812.md"


def sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def clean(value: object) -> str:
    return "" if value is None else str(value).strip()


def main() -> None:
    excluded_ids: set[str] = set()
    excluded_rows: list[dict[str, str]] = []
    with LOW_OVERLAP.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            if clean(row.get("DIFFERENCE_CLASS")) != "LOW_TEXT_OVERLAP":
                continue
            candidate_id = clean(row.get("CANDIDATE_ID"))
            if not candidate_id:
                raise SystemExit("LOW_TEXT_OVERLAP row has empty CANDIDATE_ID")
            if candidate_id in excluded_ids:
                raise SystemExit(f"duplicate excluded CANDIDATE_ID: {candidate_id}")
            excluded_ids.add(candidate_id)
            excluded_rows.append(row)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    kept_rows = 0
    excluded_written = 0
    input_rows = 0
    source_snapshot_ids: set[str] = set()
    seen_candidate_ids: set[str] = set()
    seen_identity: set[tuple[str, str]] = set()
    base_fields: list[str] = []
    excluded_fields = [
        "CANDIDATE_ID", "SITEID", "ASSETNUM", "ASSET_DESCRIPTION", "LOCATION_CODE",
        "LOCATION_DESCRIPTION", "LOCATION_PARENT", "LOCATION_SYSTEM", "TEXT_OVERLAP_SCORE",
        "EXCLUSION_REASON", "SOURCE_ROW_HASH",
    ]
    with QUALITY.open(encoding="utf-8-sig", newline="") as source, FILTERED.open("w", encoding="utf-8-sig", newline="") as kept, EXCLUDED.open("w", encoding="utf-8-sig", newline="") as excluded:
        reader = csv.DictReader(source)
        base_fields = list(reader.fieldnames or [])
        if not base_fields:
            raise SystemExit("quality input has no header")
        kept_writer = csv.DictWriter(kept, fieldnames=base_fields)
        kept_writer.writeheader()
        excluded_writer = csv.DictWriter(excluded, fieldnames=excluded_fields)
        excluded_writer.writeheader()
        for row in reader:
            input_rows += 1
            source_snapshot_ids.add(clean(row.get("SOURCE_SNAPSHOT_ID")))
            candidate_id = clean(row.get("CANDIDATE_ID"))
            identity = (clean(row.get("SITEID")), clean(row.get("ASSETNUM")))
            if candidate_id in excluded_ids:
                excluded_row = next(item for item in excluded_rows if clean(item.get("CANDIDATE_ID")) == candidate_id)
                excluded_writer.writerow(
                    {
                        "CANDIDATE_ID": candidate_id,
                        "SITEID": clean(row.get("SITEID")),
                        "ASSETNUM": clean(row.get("ASSETNUM")),
                        "ASSET_DESCRIPTION": clean(row.get("ORIGINAL_DESCRIPTION")),
                        "LOCATION_CODE": clean(row.get("LOCATION_CODE")) or clean(excluded_row.get("LOCATION_CODE")),
                        "LOCATION_DESCRIPTION": clean(row.get("LOCATION_DESCRIPTION")) or clean(excluded_row.get("LOCATION_DESCRIPTION")),
                        "LOCATION_PARENT": clean(row.get("LOCATION_PARENT")),
                        "LOCATION_SYSTEM": clean(row.get("LOCATION_SYSTEM")),
                        "TEXT_OVERLAP_SCORE": clean(excluded_row.get("TEXT_OVERLAP_SCORE")),
                        "EXCLUSION_REASON": "LOW_TEXT_OVERLAP: removed from unification processing scope; source and frozen quality batch preserved",
                        "SOURCE_ROW_HASH": clean(row.get("SOURCE_ROW_HASH")),
                    }
                )
                excluded_written += 1
                continue
            kept_writer.writerow(row)
            kept_rows += 1
            if candidate_id:
                if candidate_id in seen_candidate_ids:
                    raise SystemExit(f"duplicate kept CANDIDATE_ID: {candidate_id}")
                seen_candidate_ids.add(candidate_id)
            if identity in seen_identity:
                raise SystemExit(f"duplicate kept identity: {identity}")
            seen_identity.add(identity)

    if excluded_written != len(excluded_ids):
        raise SystemExit(f"exclusion mismatch: expected {len(excluded_ids)}, wrote {excluded_written}")
    source_snapshot_ids.discard("")
    if len(source_snapshot_ids) != 1:
        raise SystemExit(f"input quality batch must have exactly one SOURCE_SNAPSHOT_ID, got {sorted(source_snapshot_ids)}")
    source_snapshot_id = next(iter(source_snapshot_ids))

    manifest = {
        "run_id": "hd-quality-scope-exclude-low-overlap-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
        "source_snapshot_id": source_snapshot_id,
        "input_quality_file": str(QUALITY),
        "input_quality_file_sha256": sha256(QUALITY),
        "input_quality_rows": input_rows,
        "excluded_classification_file": str(LOW_OVERLAP),
        "excluded_classification_rows": len(excluded_ids),
        "exclusion_rule": "DIFFERENCE_CLASS=LOW_TEXT_OVERLAP",
        "excluded_rows": excluded_written,
        "output_quality_file": str(FILTERED),
        "output_quality_file_sha256": sha256(FILTERED),
        "output_quality_rows": kept_rows,
        "excluded_file": str(EXCLUDED),
        "excluded_file_sha256": sha256(EXCLUDED),
        "source_write": False,
        "formal_publication": False,
        "original_quality_batch_preserved": True,
        "status": "derived_unification_scope",
        "rule_version": "hd-quality-scope-0.2.0",
    }
    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    verification = {
        "status": "PASS" if kept_rows + excluded_written == input_rows and len(seen_candidate_ids) == kept_rows else "FAIL",
        "input_rows": input_rows,
        "kept_rows": kept_rows,
        "excluded_rows": excluded_written,
        "reconciled": kept_rows + excluded_written == input_rows,
        "kept_distinct_candidate_ids": len(seen_candidate_ids),
        "kept_distinct_identities": len(seen_identity),
        "source_write": False,
        "formal_publication": False,
    }
    VERIFICATION.write_text(json.dumps(verification, ensure_ascii=False, indent=2), encoding="utf-8")

    report = [
        "# 统一语义处理批次调整：排除低文本重合数据",
        "",
        "将`DIFFERENCE_CLASS=LOW_TEXT_OVERLAP`的1,260条从后续统一语义处理范围排除。",
        "",
        "## 批次结果",
        "",
        f"- 原高质量冻结批次：{input_rows:,}条",
        f"- 排除低文本重合：{excluded_written}条",
        f"- 新统一语义处理批次：{kept_rows}条",
        "- 原始数据库：未修改",
        f"- 原{input_rows:,}条质量批次：保留",
        "- 被排除数据：单独留档，后续可恢复",
        "",
        f"新处理批次：[hd_quality_candidates_for_unification.csv](../quality_scope/hd_quality_candidates_for_unification.csv)",
        f"排除清单：[excluded_low_overlap_20260812.csv](../quality_scope/excluded_low_overlap_20260812.csv)",
        f"批次清单：[manifest.json](../quality_scope/manifest.json)",
        f"校验结果：[verification.json](../quality_scope/verification.json)",
    ]
    REPORT.write_text("\n".join(report) + "\n", encoding="utf-8")
    print(json.dumps({"manifest": manifest, "verification": verification}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
