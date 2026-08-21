"""Expand inspection identity candidates with local context evidence."""
from __future__ import annotations

import argparse
import difflib
import json
import pathlib
import sqlite3

from build_identity_result_layer import norm_text
from safe_convert import to_float

ROOT = pathlib.Path(__file__).resolve().parent


def latest_result() -> pathlib.Path:
    return sorted((ROOT / "results").glob("identity-layer-v1-*/"), reverse=True)[0]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--result-root", default="")
    parser.add_argument("--output", default="")
    args = parser.parse_args()
    result_root = pathlib.Path(args.result_root).resolve() if args.result_root else latest_result()
    connection = sqlite3.connect(result_root / "identity_semantics.sqlite3")
    rows = connection.execute(
        """SELECT e.event_record_id, e.source_schema, e.source_table, e.source_row_id, e.site_id,
                  e.location_code, e.event_time, e.status, e.description, e.candidate_device_id,
                  c.match_method, c.score, c.evidence_json, c.kks_code, c.candidate_asset_number,
                  u.asset_number, u.canonical_name, u.location_code, u.parent_asset_number,
                  u.classstructure_id, fl.description, fl.parent_location,
                  (SELECT h.parent_location FROM location_hierarchy h
                    WHERE h.source_schema=u.master_source_schema AND h.site_id=u.site_id
                      AND h.location_code=u.location_code LIMIT 1) AS hierarchy_parent_location,
                  (SELECT pf.description FROM function_location pf
                    WHERE pf.source_schema=u.master_source_schema AND pf.site_id=u.site_id
                      AND pf.location_code=(SELECT h2.parent_location FROM location_hierarchy h2
                        WHERE h2.source_schema=u.master_source_schema AND h2.site_id=u.site_id
                          AND h2.location_code=u.location_code LIMIT 1) LIMIT 1) AS hierarchy_parent_description
             FROM device_event e
             LEFT JOIN device_match_candidate c
               ON c.source_schema=e.source_schema AND c.source_table_group='inspection'
              AND c.source_table=e.source_table AND c.source_row_id=e.source_row_id
             LEFT JOIN unified_device u ON u.unified_device_id=e.candidate_device_id
             LEFT JOIN function_location fl
               ON fl.source_schema=u.master_source_schema AND fl.site_id=u.site_id AND fl.location_code=u.location_code
            WHERE e.event_type='inspection' AND e.link_status='needs_review'
              AND e.candidate_device_id IS NOT NULL
            ORDER BY e.source_schema, e.site_id, e.source_table, e.source_row_id"""
    ).fetchall()
    columns = [item[0] for item in connection.execute(
        """SELECT e.event_record_id, e.source_schema, e.source_table, e.source_row_id, e.site_id,
                  e.location_code, e.event_time, e.status, e.description, e.candidate_device_id,
                  c.match_method, c.score, c.evidence_json, c.kks_code, c.candidate_asset_number,
                  u.asset_number, u.canonical_name, u.location_code, u.parent_asset_number,
                  u.classstructure_id, fl.description, fl.parent_location,
                  (SELECT h.parent_location FROM location_hierarchy h
                    WHERE h.source_schema=u.master_source_schema AND h.site_id=u.site_id
                      AND h.location_code=u.location_code LIMIT 1) AS hierarchy_parent_location,
                  (SELECT pf.description FROM function_location pf
                    WHERE pf.source_schema=u.master_source_schema AND pf.site_id=u.site_id
                      AND pf.location_code=(SELECT h2.parent_location FROM location_hierarchy h2
                        WHERE h2.source_schema=u.master_source_schema AND h2.site_id=u.site_id
                          AND h2.location_code=u.location_code LIMIT 1) LIMIT 1) AS hierarchy_parent_description
             FROM device_event e
             LEFT JOIN device_match_candidate c
               ON c.source_schema=e.source_schema AND c.source_table_group='inspection'
              AND c.source_table=e.source_table AND c.source_row_id=e.source_row_id
             LEFT JOIN unified_device u ON u.unified_device_id=e.candidate_device_id
             LEFT JOIN function_location fl
               ON fl.source_schema=u.master_source_schema AND fl.site_id=u.site_id AND fl.location_code=u.location_code
            WHERE e.event_type='inspection' AND e.link_status='needs_review'
              AND e.candidate_device_id IS NOT NULL
            ORDER BY e.source_schema, e.site_id, e.source_table, e.source_row_id"""
    ).description]
    report = []
    for row in rows:
        item = dict(zip(columns, row))
        try:
            item["evidence"] = json.loads(item.pop("evidence_json") or "{}")
        except json.JSONDecodeError:
            item["evidence"] = {"raw": item.pop("evidence_json", "")}
        description = norm_text(item.get("description"))
        canonical_name = norm_text(item.get("canonical_name"))
        item["description_norm_equal"] = bool(description and canonical_name and description == canonical_name)
        item["description_similarity"] = round(difflib.SequenceMatcher(None, description, canonical_name).ratio(), 4) if description and canonical_name else 0.0
        item["location_device_count"] = connection.execute(
            "SELECT COUNT(*) FROM unified_device WHERE master_source_schema=? AND site_id=? AND location_code=?",
            (item.get("source_schema"), item.get("site_id"), item.get("location_code")),
        ).fetchone()[0]
        item["location_hierarchy_count"] = connection.execute(
            "SELECT COUNT(*) FROM location_hierarchy WHERE source_schema=? AND site_id=? AND location_code=?",
            (item.get("source_schema"), item.get("site_id"), item.get("location_code")),
        ).fetchone()[0]
        if item.get("match_method") == "exact_location_and_description" and to_float(item.get("score")) >= 0.95:
            item["recommended_action"] = "candidate_for_auto_accept_after_replay"
        elif (
            item.get("match_method") == "exact_location"
            and item.get("location_device_count") == 1
            and item.get("location_hierarchy_count", 0) >= 1
            and item.get("description_similarity", 0) >= 0.9
        ):
            item["recommended_action"] = "candidate_for_auto_accept_after_replay"
        elif item.get("match_method") == "exact_location" and to_float(item.get("score")) >= 0.86:
            item["recommended_action"] = "needs_location_hierarchy_or_kks_confirmation"
        else:
            item["recommended_action"] = "keep_isolated"
        report.append(item)
    connection.close()
    output = pathlib.Path(args.output).resolve() if args.output else result_root / "inspection_identity_candidates.json"
    output.write_text(json.dumps({"count": len(report), "candidates": report, "read_only": True}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"count": len(report), "output": str(output), "recommendations": {action: sum(1 for item in report if item["recommended_action"] == action) for action in sorted({item["recommended_action"] for item in report})}, "read_only": True}, ensure_ascii=False))


if __name__ == "__main__":
    main()
