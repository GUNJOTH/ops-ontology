"""Read-only query helper for the local device semantic context layer."""
from __future__ import annotations

import argparse
import json
import pathlib
import sqlite3

ROOT = pathlib.Path(__file__).resolve().parent


def latest_result() -> pathlib.Path:
    return sorted((ROOT / "results").glob("identity-layer-v1-*/"), reverse=True)[0]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--result-root", default="")
    parser.add_argument("--schema", default="")
    parser.add_argument("--site", default="")
    parser.add_argument("--asset-number", default="")
    parser.add_argument("--term", default="")
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()
    result_root = pathlib.Path(args.result_root).resolve() if args.result_root else latest_result()
    connection = sqlite3.connect(result_root / "identity_semantics.sqlite3")
    payload: dict[str, object] = {"result_root": str(result_root), "read_only": True}
    if args.asset_number:
        payload["device"] = [list(row) for row in connection.execute(
            """SELECT unified_device_id, source_schema, site_id, asset_number, canonical_name,
                      asset_location_code, location_description, parent_location, classstructure_id,
                      latest_inspection_id, latest_inspection_time, latest_inspection_status,
                      latest_inspection_result, latest_inspection_candidate_id,
                      latest_inspection_candidate_time, latest_inspection_candidate_status,
                      latest_inspection_candidate_result
                 FROM v_device_semantic_context
                WHERE source_schema=? AND site_id=? AND asset_number=?""",
            (args.schema, args.site, args.asset_number),
        )]
    if args.term:
        like = f"%{args.term}%"
        payload["term_matches"] = [list(row) for row in connection.execute(
            """SELECT u.unified_device_id, u.master_source_schema, u.site_id, u.asset_number,
                      u.canonical_name, u.location_code, f.description, f.parent_location,
                      v.latest_inspection_id, v.latest_inspection_time, v.latest_inspection_status,
                      v.latest_inspection_candidate_id, v.latest_inspection_candidate_status
                 FROM unified_device u
                 LEFT JOIN function_location f
                   ON f.source_schema=u.master_source_schema AND f.site_id=u.site_id AND f.location_code=u.location_code
                 LEFT JOIN v_device_semantic_context v ON v.unified_device_id=u.unified_device_id
                WHERE u.canonical_name LIKE ? OR f.description LIKE ?
                ORDER BY u.master_source_schema, u.site_id, u.asset_number
                LIMIT ?""",
            (like, like, args.limit),
        )]
    payload["event_counts"] = [list(row) for row in connection.execute(
        "SELECT event_type, link_status, COUNT(*) FROM device_event GROUP BY 1,2 ORDER BY 1,2"
    )]
    connection.close()
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
