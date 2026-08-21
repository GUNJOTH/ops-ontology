"""Create a local inspection-to-device bridge registry and fillable template."""
from __future__ import annotations

import argparse
import csv
import json
import pathlib
import sqlite3
from datetime import datetime, timezone


NUMBER_FIELDS = (
    ("XJJL", "XJJLNUM"),
    ("CHECKS", "CHECKNUM"),
    ("CHECKITEM", "CHECKNUM"),
    ("ST_USECURITYCHECK", "ST_USECURITYCHECKNUM"),
    ("INVENTORYCHECK", "INVCHECKNUM"),
    ("INVENTORYCHECKMAIN", "INVCHECKNUM"),
    ("PLUSCSPOTCHECK", "PLUSCSPOTCHECKID"),
)


def select_record_number(source_table: str, fields: dict[str, str]) -> tuple[str, str]:
    for table, field in NUMBER_FIELDS:
        if source_table == table and fields.get(field):
            return field, fields[field]
    for field, value in fields.items():
        if value and (field.endswith("NUM") or field.endswith("ID")):
            return field, value
    return "", ""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--result-root", required=True)
    parser.add_argument("--analysis", required=True)
    parser.add_argument("--output-csv", default="")
    args = parser.parse_args()
    result_root = pathlib.Path(args.result_root).resolve()
    analysis_path = pathlib.Path(args.analysis).resolve()
    payload = json.loads(analysis_path.read_text(encoding="utf-8"))
    created_at = datetime.now(timezone.utc).isoformat()
    output_csv = pathlib.Path(args.output_csv).resolve() if args.output_csv else result_root / "inspection-device-bridge-template.csv"
    connection = sqlite3.connect(result_root / "identity_semantics.sqlite3")
    connection.execute(
        """CREATE TABLE IF NOT EXISTS inspection_device_bridge (
             bridge_id TEXT PRIMARY KEY,
             event_record_id TEXT NOT NULL UNIQUE,
             source_schema TEXT NOT NULL,
             source_table TEXT NOT NULL,
             source_row_id TEXT NOT NULL,
             site_id TEXT,
             location_code TEXT,
             record_number_type TEXT,
             record_number TEXT,
             parent_location TEXT,
             source_location_as_kks_candidate TEXT,
             asset_number TEXT,
             kks_code TEXT,
             mapping_status TEXT NOT NULL,
             validation_status TEXT NOT NULL,
             mapping_evidence_json TEXT NOT NULL,
             reviewer TEXT,
             review_reason TEXT,
             created_at TEXT NOT NULL,
             updated_at TEXT NOT NULL
           )"""
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_inspection_device_bridge_status ON inspection_device_bridge(mapping_status, validation_status)"
    )
    connection.execute("DROP VIEW IF EXISTS v_inspection_device_bridge")
    connection.execute(
        """CREATE VIEW v_inspection_device_bridge AS
             SELECT b.bridge_id, b.event_record_id, b.source_schema, b.source_table,
                    b.source_row_id, b.site_id, b.location_code, b.record_number_type,
                    b.record_number, b.parent_location, b.source_location_as_kks_candidate,
                    b.asset_number, b.kks_code, b.mapping_status, b.validation_status,
                    e.event_time, e.status AS event_status, e.description AS event_description,
                    u.unified_device_id, u.canonical_name, u.location_code AS device_location_code,
                    u.status AS device_status
               FROM inspection_device_bridge b
               LEFT JOIN device_event e ON e.event_record_id=b.event_record_id
               LEFT JOIN unified_device u
                 ON u.master_source_schema=b.source_schema
                AND u.site_id=b.site_id
                AND u.asset_number=b.asset_number"""
    )
    columns = [
        "event_record_id", "source_schema", "source_table", "source_row_id", "site_id", "location_code",
        "record_number_type", "record_number", "parent_location", "source_location_as_kks_candidate",
        "asset_number", "kks_code", "mapping_status", "validation_status", "mapping_evidence_note", "reviewer", "review_reason",
    ]
    with output_csv.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for row in payload.get("rows", []):
            evidence = row.get("evidence") or {}
            source_row = evidence.get("fresh_source_row") or {}
            number_type, number = select_record_number(row.get("source_table", ""), source_row)
            parents = "|".join(evidence.get("raw_parent_locations", []))
            source_kks_candidate = row.get("location_code", "") if evidence.get("location_kks_format_signal") else ""
            note = (
                "业务记录号仅作追溯；请补充 ASSETNUM 或 KKS 后再校验。"
                if number else
                "请补充 ASSETNUM 或 KKS；当前位置只能作为上下文。"
            )
            csv_row = {
                "event_record_id": row.get("event_record_id", ""),
                "source_schema": row.get("source_schema", ""),
                "source_table": row.get("source_table", ""),
                "source_row_id": row.get("source_row_id", ""),
                "site_id": row.get("site_id", ""),
                "location_code": row.get("location_code", ""),
                "record_number_type": number_type,
                "record_number": number,
                "parent_location": parents,
                "source_location_as_kks_candidate": source_kks_candidate,
                "asset_number": "",
                "kks_code": "",
                "mapping_status": "pending",
                "validation_status": "not_validated",
                "mapping_evidence_note": note,
                "reviewer": "",
                "review_reason": "",
            }
            writer.writerow(csv_row)
            compact_evidence = {
                "record_number_type": number_type,
                "record_number": number,
                "parent_locations": evidence.get("raw_parent_locations", []),
                "source_location_as_kks_candidate": source_kks_candidate,
                "analysis_id": analysis_path.stem,
                "source_write": False,
                "formal_publication": False,
            }
            connection.execute(
                """INSERT INTO inspection_device_bridge
                   (bridge_id,event_record_id,source_schema,source_table,source_row_id,site_id,location_code,
                    record_number_type,record_number,parent_location,source_location_as_kks_candidate,
                    asset_number,kks_code,mapping_status,validation_status,mapping_evidence_json,reviewer,review_reason,created_at,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(bridge_id) DO UPDATE SET
                    event_record_id=excluded.event_record_id,
                    source_schema=excluded.source_schema,
                    source_table=excluded.source_table,
                    source_row_id=excluded.source_row_id,
                    site_id=excluded.site_id,
                    location_code=excluded.location_code,
                    record_number_type=excluded.record_number_type,
                    record_number=excluded.record_number,
                    parent_location=excluded.parent_location,
                    source_location_as_kks_candidate=excluded.source_location_as_kks_candidate,
                    asset_number=excluded.asset_number,
                    kks_code=excluded.kks_code,
                    mapping_status=excluded.mapping_status,
                    validation_status=excluded.validation_status,
                    mapping_evidence_json=excluded.mapping_evidence_json,
                    reviewer=excluded.reviewer,
                    review_reason=excluded.review_reason,
                    updated_at=excluded.updated_at""",
                (
                    f"IB-{row.get('event_record_id')}", row.get("event_record_id", ""), row.get("source_schema", ""),
                    row.get("source_table", ""), row.get("source_row_id", ""), row.get("site_id", ""), row.get("location_code", ""),
                    number_type, number, parents, source_kks_candidate, "", "", "pending", "not_validated",
                    json.dumps(compact_evidence, ensure_ascii=False, sort_keys=True), "", "", created_at, created_at,
                ),
            )
    connection.commit()
    counts = connection.execute(
        "SELECT mapping_status, COUNT(*) FROM inspection_device_bridge GROUP BY 1 ORDER BY 1"
    ).fetchall()
    connection.close()
    print(json.dumps({
        "registry_rows": sum(count for _, count in counts),
        "mapping_status": dict(counts),
        "template": str(output_csv),
        "source_write": False,
        "formal_publication": False,
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
