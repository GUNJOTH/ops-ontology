"""Build a deterministic, stratified 300-row semantic review sample."""
from __future__ import annotations

import csv
import hashlib
import json
import pathlib
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parent
FROZEN_MANIFEST = ROOT / "frozen" / "manifest.json"
QUALITY_FILE = ROOT / "quality" / "hd_quality_candidates.csv"
SAMPLE_DIR = ROOT / "samples"
SAMPLE_FILE = SAMPLE_DIR / "semantic_sample_300.csv"
SAMPLE_MANIFEST = SAMPLE_DIR / "manifest.json"
TARGET = 300


def main() -> None:
    frozen = json.loads(FROZEN_MANIFEST.read_text(encoding="utf-8"))
    current_hash = hashlib.sha256(QUALITY_FILE.read_bytes()).hexdigest()
    if current_hash != frozen["quality_file_sha256"]:
        raise SystemExit("冻结批次哈希不一致，停止抽样")
    by_site: defaultdict[str, defaultdict[str, list[tuple[str, dict[str, str]]]]] = defaultdict(lambda: defaultdict(list))
    with QUALITY_FILE.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            context = json.loads(row.get("CONTEXT_JSON", "{}"))
            class_context = context.get("class_structure", {})
            classification_context = context.get("classification", {})
            location_context = context.get("location", {})
            row["CLASSSTRUCTUREID"] = row.get("CLASSSTRUCTUREID") or class_context.get("CLASSSTRUCTUREID", "")
            row["CLASSSTRUCTURE_DESCRIPTION"] = row.get("CLASSSTRUCTURE_DESCRIPTION") or class_context.get("DESCRIPTION", "")
            row["CLASSIFICATION_DESCRIPTION"] = row.get("CLASSIFICATION_DESCRIPTION") or classification_context.get("DESCRIPTION", "")
            row["LOCATION_CLASSSTRUCTUREID"] = location_context.get("CLASSSTRUCTUREID", "")
            if row.get("CLASSSTRUCTUREID"):
                device_type = "ASSET_CLASS|" + (row.get("CLASSSTRUCTURE_DESCRIPTION") or row.get("CLASSSTRUCTUREID"))
            elif row.get("LOCATION_CLASSSTRUCTUREID"):
                device_type = "LOCATION_CLASS|" + row.get("LOCATION_CLASSSTRUCTUREID")
            else:
                device_type = "UNCLASSIFIED"
            stratum = f"{row.get('SITEID', '')}|{row.get('CLASSSTRUCTUREID', '')}|{device_type}"
            digest = hashlib.sha256((row.get("CANDIDATE_ID", "") + "|semantic-sample-v1").encode("utf-8")).hexdigest()
            bucket = by_site[row.get("SITEID", "")][stratum]
            bucket.append((digest, row))
    for site_groups in by_site.values():
        for stratum, items in site_groups.items():
            site_groups[stratum] = sorted(items, key=lambda item: item[0])[:50]

    selected: list[tuple[str, dict[str, str], str]] = []
    sites = sorted(by_site)
    strata_by_site = {site: sorted(by_site[site]) for site in sites}
    for rank in range(50):
        for site in sites:
            for stratum in strata_by_site[site]:
                items = by_site[site][stratum]
                if rank < len(items) and len(selected) < TARGET:
                    digest, row = items[rank]
                    selected.append((digest, row, stratum))
        if len(selected) >= TARGET:
            break
    selected.sort(key=lambda item: (item[1].get("SITEID", ""), item[2], item[0]))
    SAMPLE_DIR.mkdir(parents=True, exist_ok=True)
    fields = [
        "SAMPLE_ID", "STRATUM", "SITEID", "ASSETNUM", "ASSETID", "CANDIDATE_ID", "ORIGINAL_DESCRIPTION",
        "CANDIDATE_DESCRIPTION", "CLASSSTRUCTUREID", "CLASSSTRUCTURE_DESCRIPTION", "CLASSIFICATION_DESCRIPTION",
        "LOCATION_CLASSSTRUCTUREID", "LOCATION", "LOCATION_DESCRIPTION", "LOCATION_STATUS", "LOCATION_PARENT", "SPEC_COUNT", "PARENT_ASSET_COUNT",
        "KKS_CANDIDATE", "KKS_SOURCE_FIELD", "KKS_HEURISTIC", "KKS_REVIEW_DECISION",
        "CONTEXT_JSON", "SOURCE_ROW_HASH", "CONTEXT_HASH", "EXPECTED_UNIFIED_DESCRIPTION", "REVIEW_DECISION", "REVIEW_NOTES",
    ]
    site_counts: Counter[str] = Counter()
    stratum_counts: Counter[str] = Counter()
    with SAMPLE_FILE.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for index, (_, row, stratum) in enumerate(selected, start=1):
            out = {field: row.get(field, "") for field in fields}
            out["SAMPLE_ID"] = f"HD-SAMPLE-{index:04d}"
            out["STRATUM"] = stratum
            context = json.loads(row.get("CONTEXT_JSON", "{}"))
            kks_value = context.get("location", {}).get("LOCATION", "") or row.get("LOCATION", "")
            out["KKS_CANDIDATE"] = kks_value
            out["KKS_SOURCE_FIELD"] = "LOCATIONS.LOCATION"
            out["KKS_HEURISTIC"] = "1" if re.fullmatch(r"[0-9][0-9A-Z]{10,16}", kks_value) else "0"
            out["KKS_REVIEW_DECISION"] = "pending"
            out["EXPECTED_UNIFIED_DESCRIPTION"] = ""
            out["REVIEW_DECISION"] = "pending"
            out["REVIEW_NOTES"] = ""
            writer.writerow(out)
            site_counts[row.get("SITEID", "")] += 1
            stratum_counts[stratum] += 1
    manifest = {
        "sample_run_id": "hd-semantic-sample-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
        "source_snapshot_id": frozen["source_snapshot_id"],
        "frozen_quality_file_sha256": frozen["quality_file_sha256"],
        "sample_rows": len(selected),
        "target_rows": TARGET,
        "strata": ["SITEID", "CLASSSTRUCTUREID", "CLASSSTRUCTURE_DESCRIPTION", "CLASSIFICATION_DESCRIPTION", "LOCATION_CLASSSTRUCTUREID"],
        "site_counts": dict(site_counts),
        "stratum_count": len(stratum_counts),
        "review_decision": "pending",
        "output": str(SAMPLE_FILE),
        "source_write": False,
        "formal_publication": False,
    }
    SAMPLE_MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(manifest)


if __name__ == "__main__":
    main()
