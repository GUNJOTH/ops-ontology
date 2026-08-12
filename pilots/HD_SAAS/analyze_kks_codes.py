"""Profile KKS-like location codes from the HD read-only context snapshot."""
from __future__ import annotations

import csv
import json
import pathlib
import re
from collections import Counter
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parent
LOCATIONS = ROOT / "context" / "locations.csv"
OUTPUT = ROOT / "reports" / "kks_profile.json"


def main() -> None:
    rows = 0
    location_values: list[str] = []
    location_code_values: list[str] = []
    system_values: list[str] = []
    examples: list[dict[str, str]] = []
    lengths: Counter[str] = Counter()
    pattern_counts: Counter[str] = Counter()
    hierarchy_examples: list[dict[str, str]] = []
    with LOCATIONS.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            rows += 1
            location = (row.get("LOCATION") or "").strip()
            location_code = (row.get("LOCATIONSCODE") or "").strip()
            system = (row.get("C_SYSTEM") or "").strip()
            if location:
                location_values.append(location)
                lengths[str(len(location))] += 1
                if re.fullmatch(r"[0-9][0-9A-Z]{10,16}", location):
                    pattern_counts["KKS_LIKE_HEURISTIC"] += 1
                    if len(examples) < 20:
                        examples.append({"location": location, "description": row.get("DESCRIPTION", ""), "system": system})
                if re.fullmatch(r"[A-Za-z0-9]+", location):
                    pattern_counts["ALNUM_ONLY"] += 1
                if re.search(r"[A-Za-z]", location) and re.search(r"[0-9]", location):
                    pattern_counts["ALPHA_DIGIT"] += 1
            if location_code:
                location_code_values.append(location_code)
            if system:
                system_values.append(system)
            if row.get("LOCATION") and row.get("LEVEL1"):
                if len(hierarchy_examples) < 20:
                    hierarchy_examples.append({"location": location, "level1": row.get("LEVEL1", ""), "level2": row.get("LEVEL2", "")})
    result = {
        "profile_run_id": "hd-kks-profile-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
        "source_schema": "HD_SAAS",
        "source_table": "LOCATIONS",
        "rows_profiled": rows,
        "location_nonempty": len(location_values),
        "location_unique": len(set(location_values)),
        "locationcode_nonempty": len(location_code_values),
        "csystem_nonempty": len(system_values),
        "length_counts": dict(lengths),
        "pattern_counts": dict(pattern_counts),
        "location_examples": examples,
        "hierarchy_field_examples": hierarchy_examples,
        "conclusion": "LOCATIONS.LOCATION is a strong KKS-like candidate; formal KKS decoding requires a plant-specific key-part catalogue.",
        "source_write": False,
        "formal_publication": False,
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(result)


if __name__ == "__main__":
    main()
