"""Validate a filled inspection-to-device bridge template without applying it."""
from __future__ import annotations

import argparse
import csv
import json
import pathlib
import sqlite3
from collections import Counter


def clean(value: object) -> str:
    return "" if value is None else str(value).strip()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--result-root", required=True)
    parser.add_argument("--template", default="")
    parser.add_argument("--output", default="")
    args = parser.parse_args()
    result_root = pathlib.Path(args.result_root).resolve()
    template = pathlib.Path(args.template).resolve() if args.template else result_root / "inspection-device-bridge-template.csv"
    output = pathlib.Path(args.output).resolve() if args.output else result_root / "inspection-device-bridge-validation.json"
    connection = sqlite3.connect(result_root / "identity_semantics.sqlite3")
    report_rows = []
    summary = Counter()
    with template.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            event_id = clean(row.get("event_record_id"))
            event = connection.execute(
                "SELECT source_schema, source_table, source_row_id, site_id, location_code, link_status FROM device_event WHERE event_record_id=?",
                (event_id,),
            ).fetchone()
            schema = clean(row.get("source_schema"))
            site = clean(row.get("site_id"))
            asset_number = clean(row.get("asset_number"))
            kks_code = clean(row.get("kks_code"))
            reasons = []
            asset_devices = connection.execute(
                """SELECT unified_device_id, asset_number, location_code, canonical_name, status
                     FROM unified_device
                    WHERE master_source_schema=? AND site_id=? AND asset_number=?""",
                (schema, site, asset_number),
            ).fetchall() if asset_number else []
            kks_devices = connection.execute(
                """SELECT unified_device_id, asset_number, location_code, canonical_name, status
                     FROM unified_device
                    WHERE master_source_schema=? AND site_id=? AND location_code=?""",
                (schema, site, kks_code),
            ).fetchall() if kks_code else []
            target_devices = asset_devices or kks_devices
            if not event:
                reasons.append("event_not_found")
            elif event[5] != "blocked":
                reasons.append("event_not_blocked")
            if not asset_number and not kks_code:
                reasons.append("missing_assetnum_and_kks")
            if asset_number and len(asset_devices) != 1:
                reasons.append("assetnum_not_unique_or_not_found")
            if kks_code and len(kks_devices) != 1:
                reasons.append("kks_not_unique_or_not_found")
            if asset_number and kks_code and len(asset_devices) == 1 and len(kks_devices) == 1 and asset_devices[0][0] != kks_devices[0][0]:
                reasons.append("assetnum_kks_conflict")
            if not reasons and len(target_devices) == 1:
                decision = "candidate_for_approval"
            else:
                decision = "blocked"
            summary[decision] += 1
            report_rows.append({
                "event_record_id": event_id,
                "source_schema": schema,
                "source_table": clean(row.get("source_table")),
                "source_row_id": clean(row.get("source_row_id")),
                "site_id": site,
                "asset_number": asset_number,
                "kks_code": kks_code,
                "decision": decision,
                "reasons": sorted(set(reasons)),
                "asset_candidates": [dict(item) for item in asset_devices],
                "kks_candidates": [dict(item) for item in kks_devices],
                "mapping_status": clean(row.get("mapping_status")),
            })
    connection.close()
    payload = {
        "template": str(template),
        "count": len(report_rows),
        "summary": dict(sorted(summary.items())),
        "rows": report_rows,
        "policy": {
            "source_write": False,
            "formal_publication": False,
            "apply_requires_explicit_approval": True,
            "kks_is_validated_against_source_location_code": True,
        },
    }
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"count": len(report_rows), "summary": dict(sorted(summary.items())), "output": str(output), "source_write": False}, ensure_ascii=False))


if __name__ == "__main__":
    main()
