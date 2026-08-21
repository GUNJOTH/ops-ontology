"""Analyze blocked inspection identity links using only the local result layer."""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sqlite3
from collections import Counter

ROOT = pathlib.Path(__file__).resolve().parent


def latest_result() -> pathlib.Path:
    return sorted((ROOT / "results").glob("identity-layer-v1-*/"), reverse=True)[0]


def clean(value: object) -> str:
    return "" if value is None else str(value).strip()


def norm(value: object) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", clean(value).casefold())


def kks_like(value: object) -> bool:
    text = clean(value).upper()
    if not text or len(text) < 6 or len(text) > 32:
        return False
    # This is only a format signal.  LOCATION is not relabeled as KKS unless
    # the source schema explicitly provides a KKS field.
    return bool(re.fullmatch(r"[A-Z0-9][A-Z0-9._/-]*", text)) and bool(re.search(r"[A-Z]", text)) and bool(re.search(r"\d", text))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--result-root", default="")
    parser.add_argument("--output", default="")
    args = parser.parse_args()
    result_root = pathlib.Path(args.result_root).resolve() if args.result_root else latest_result()
    db_path = result_root / "identity_semantics.sqlite3"
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """
        SELECT e.event_record_id, e.source_schema, e.source_table, e.source_row_id,
               e.site_id, e.location_code, e.event_time, e.status, e.description,
               e.candidate_device_id, c.source_key_type, c.source_key, c.kks_code,
               c.candidate_asset_number, c.score, c.match_method, c.decision,
               c.evidence_json
          FROM device_event e
          LEFT JOIN device_match_candidate c
            ON c.source_schema=e.source_schema
           AND c.source_table_group='inspection'
           AND c.source_table=e.source_table
           AND c.source_row_id=e.source_row_id
         WHERE e.event_type='inspection' AND e.link_status='blocked'
         ORDER BY e.source_schema, e.source_table, e.site_id, e.source_row_id
        """
    ).fetchall()
    report_rows = []
    summary = Counter()
    for row in rows:
        item = dict(row)
        location = clean(item.get("location_code"))
        description = clean(item.get("description"))
        site = clean(item.get("site_id"))
        schema = clean(item.get("source_schema"))
        location_devices = con.execute(
            """SELECT unified_device_id, asset_number, canonical_name, parent_asset_number,
                      location_code, classstructure_id, status
                 FROM unified_device
                WHERE master_source_schema=? AND site_id=? AND location_code=?
                ORDER BY asset_number""",
            (schema, site, location),
        ).fetchall() if location else []
        location_hierarchy = con.execute(
            """SELECT parent_location
                 FROM location_hierarchy
                WHERE source_schema=? AND site_id=? AND location_code=?
                ORDER BY parent_location""",
            (schema, site, location),
        ).fetchall() if location else []
        location_rows = con.execute(
            """SELECT location_code, description, parent_location
                 FROM function_location
                WHERE source_schema=? AND site_id=? AND location_code=?
                ORDER BY location_code""",
            (schema, site, location),
        ).fetchall() if location else []
        try:
            evidence = json.loads(item.pop("evidence_json") or "{}")
        except json.JSONDecodeError:
            evidence = {"raw": item.pop("evidence_json", "")}
        candidate_names = [clean(d[2]) for d in location_devices if clean(d[2])]
        exact_name_count = sum(1 for name in candidate_names if norm(name) == norm(description) and norm(description))
        item.update(
            {
                "evidence": evidence,
                "location_kks_format_signal": kks_like(location),
                "location_device_count": len(location_devices),
                "location_hierarchy_count": len(location_hierarchy),
                "function_location_count": len(location_rows),
                "exact_location_description_count": exact_name_count,
                "location_devices": [dict(d) for d in location_devices[:20]],
                "location_hierarchy": [dict(h) for h in location_hierarchy[:20]],
                "function_location": [dict(f) for f in location_rows[:5]],
            }
        )
        if not location:
            action = "blocked_missing_location"
        elif len(location_devices) == 1 and len(location_hierarchy) >= 1 and exact_name_count == 1:
            action = "candidate_exact_location_description_parent"
        elif len(location_devices) == 1 and len(location_hierarchy) >= 1:
            action = "candidate_unique_location_parent_description_review"
        elif len(location_devices) > 1 and exact_name_count == 1 and len(location_hierarchy) >= 1:
            action = "candidate_description_disambiguates_location_review"
        elif len(location_devices) > 1:
            action = "blocked_location_not_unique"
        elif len(location_devices) == 0 and len(location_hierarchy) >= 1:
            action = "blocked_location_without_asset"
        else:
            action = "blocked_no_local_context"
        item["recommended_action"] = action
        summary[action] += 1
        summary[f"table:{schema}:{item.get('source_table')}"] += 1
        report_rows.append(item)
    con.close()
    output = pathlib.Path(args.output).resolve() if args.output else result_root / "blocked-inspection-analysis.json"
    payload = {
        "count": len(report_rows),
        "summary": dict(sorted(summary.items())),
        "kks_format_signal_count": sum(1 for item in report_rows if item["location_kks_format_signal"]),
        "rows": report_rows,
        "policy": {
            "source_write": False,
            "formal_publication": False,
            "location_is_kks_only_as_format_signal": True,
            "auto_accept_requires_unique_device_parent_and_description_evidence": True,
        },
    }
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"count": len(report_rows), "summary": dict(sorted(summary.items())), "output": str(output), "read_only": True}, ensure_ascii=False))


if __name__ == "__main__":
    main()
