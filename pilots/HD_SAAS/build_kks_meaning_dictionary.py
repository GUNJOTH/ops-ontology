"""Build a read-only KKS/location meaning dictionary for the frozen HD quality batch."""
from __future__ import annotations

import csv
import hashlib
import json
import pathlib
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parent
QUALITY = ROOT / "quality" / "hd_quality_candidates.csv"
QUALITY_MANIFEST = ROOT / "quality" / "manifest.json"
LOCATIONS = ROOT / "context" / "locations.csv"
OUTPUT_DIR = ROOT / "kks_dictionary"
OUTPUT = OUTPUT_DIR / "kks_code_meaning_dictionary.csv"
MANIFEST = OUTPUT_DIR / "manifest.json"

KKS_PATTERN = re.compile(r"[0-9][0-9A-Z]{10,16}")


def file_sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def add_counter(mapping: dict[tuple[str, str], Counter[str]], key: tuple[str, str], value: str) -> None:
    value = (value or "").strip()
    if value:
        mapping[key][value] += 1


def main() -> None:
    # Supporting location metadata remains a local read-only snapshot.
    location_meta: dict[tuple[str, str], dict[str, str]] = {}
    with LOCATIONS.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            site = (row.get("SITEID") or "").strip()
            code = (row.get("LOCATION") or "").strip()
            if site and code:
                location_meta[(site, code)] = {
                    "description": (row.get("DESCRIPTION") or "").strip(),
                    "type": (row.get("TYPE") or "").strip(),
                    "status": (row.get("STATUS") or "").strip(),
                    "classstructureid": (row.get("CLASSSTRUCTUREID") or "").strip(),
                    "system": (row.get("C_SYSTEM") or "").strip(),
                }

    descriptions: dict[tuple[str, str], Counter[str]] = defaultdict(Counter)
    parents: dict[tuple[str, str], Counter[str]] = defaultdict(Counter)
    asset_counts: Counter[tuple[str, str]] = Counter()
    candidate_ids: dict[tuple[str, str], set[str]] = defaultdict(set)
    quality_rows = 0
    quality_sites: set[str] = set()
    with QUALITY.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            quality_rows += 1
            site = (row.get("SITEID") or "").strip()
            quality_sites.add(site)
            try:
                context = json.loads(row.get("CONTEXT_JSON") or "{}")
            except json.JSONDecodeError:
                continue
            location = context.get("location") or {}
            code = (location.get("LOCATION") or "").strip()
            if not site or not code:
                continue
            key = (site, code)
            add_counter(descriptions, key, row.get("LOCATION_DESCRIPTION") or location.get("DESCRIPTION", ""))
            add_counter(parents, key, row.get("LOCATION_PARENT") or "")
            asset_counts[key] += 1
            candidate_id = (row.get("CANDIDATE_ID") or "").strip()
            if candidate_id:
                candidate_ids[key].add(candidate_id)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "SITEID",
        "KKS_CODE",
        "KKS_MEANING",
        "KKS_MEANING_STATUS",
        "KKS_PARENT_CODE",
        "KKS_PARENT_MEANING",
        "C_SYSTEM",
        "LOCATION_TYPE",
        "LOCATION_STATUS",
        "CLASSSTRUCTUREID",
        "LINKED_HIGH_QUALITY_ASSET_COUNT",
        "DESCRIPTION_VARIANT_COUNT",
        "EVIDENCE_SOURCE",
        "FORMAL_KKS_DECODE_STATUS",
        "RULE_VERSION",
    ]
    rows_written = 0
    direct_meaning = 0
    unresolved = 0
    conflicts = 0
    with OUTPUT.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for site, code in sorted(asset_counts):
            key = (site, code)
            meta = location_meta.get(key, {})
            desc_counter = descriptions.get(key, Counter())
            parent_counter = parents.get(key, Counter())
            meaning = desc_counter.most_common(1)[0][0] if desc_counter else meta.get("description", "")
            parent_code = parent_counter.most_common(1)[0][0] if parent_counter else ""
            parent_meta = location_meta.get((site, parent_code), {}) if parent_code else {}
            variant_count = len(desc_counter)
            if variant_count > 1:
                meaning_status = "CONFLICTING_DESCRIPTION_VARIANTS"
                conflicts += 1
            elif meaning:
                meaning_status = "DIRECT_LOCATION_DESCRIPTION"
                direct_meaning += 1
            else:
                meaning_status = "UNRESOLVED_NO_DESCRIPTION"
                unresolved += 1
            writer.writerow(
                {
                    "SITEID": site,
                    "KKS_CODE": code,
                    "KKS_MEANING": meaning,
                    "KKS_MEANING_STATUS": meaning_status,
                    "KKS_PARENT_CODE": parent_code,
                    "KKS_PARENT_MEANING": parent_meta.get("description", ""),
                    "C_SYSTEM": meta.get("system", ""),
                    "LOCATION_TYPE": meta.get("type", ""),
                    "LOCATION_STATUS": meta.get("status", ""),
                    "CLASSSTRUCTUREID": meta.get("classstructureid", ""),
                    "LINKED_HIGH_QUALITY_ASSET_COUNT": len(candidate_ids.get(key, set())) or asset_counts[key],
                    "DESCRIPTION_VARIANT_COUNT": variant_count,
                    "EVIDENCE_SOURCE": "HD_SAAS.LOCATIONS.DESCRIPTION + ASSET.LOCATION -> LOCATIONS.LOCATION",
                    "FORMAL_KKS_DECODE_STATUS": "NOT_FORMALLY_DECODED_WITHOUT_PLANT_KEY_PART_CATALOG",
                    "RULE_VERSION": "hd-kks-meaning-dictionary-0.1.0",
                }
            )
            rows_written += 1

    with QUALITY_MANIFEST.open(encoding="utf-8-sig") as handle:
        quality_manifest = json.load(handle)
    result = {
        "dictionary_run_id": "hd-kks-meaning-dictionary-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
        "source_schema": "HD_SAAS",
        "source_table": "LOCATIONS",
        "scope": "frozen HD quality batch only",
        "source_quality_file": str(QUALITY),
        "source_quality_file_sha256": file_sha256(QUALITY),
        "source_quality_manifest_sha256": quality_manifest.get("output_sha256", ""),
        "quality_rows_profiled": quality_rows,
        "quality_sites": sorted(site for site in quality_sites if site),
        "kks_codes_with_high_quality_links": rows_written,
        "direct_location_meaning_rows": direct_meaning,
        "unresolved_no_description_rows": unresolved,
        "conflicting_description_variant_rows": conflicts,
        "formal_kks_decoding": False,
        "source_write": False,
        "formal_publication": False,
        "status": "candidate_dictionary_only",
        "rule_version": "hd-kks-meaning-dictionary-0.1.0",
    }
    MANIFEST.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
