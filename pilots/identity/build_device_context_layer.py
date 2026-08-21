"""Extend the local identity DB with location, equipment and event context.

This is a relational semantic layer for queries such as:
location -> device -> inspection/defect/work order -> latest inspection.
It consumes only local snapshots and never connects to or writes the source DB.
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
import pathlib
import sqlite3
import sys
from datetime import datetime, timezone

from build_identity_result_layer import direct_key, event_description, event_row_id, read_operational_files

ROOT = pathlib.Path(__file__).resolve().parent
REPO_ROOT = ROOT.parents[2]


def sha_id(prefix: str, *parts: str) -> str:
    return prefix + hashlib.sha256("|".join(str(part or "") for part in parts).encode("utf-8")).hexdigest()[:24]


def clean(value: object) -> str:
    return "" if value is None else str(value).strip()


def latest_snapshot(pattern: str) -> pathlib.Path:
    matches = sorted((ROOT / "snapshots").glob(pattern), reverse=True)
    if not matches:
        raise SystemExit(f"No snapshot matches {pattern}")
    return matches[0]


def make_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS function_location (
            location_record_id TEXT PRIMARY KEY,
            source_schema TEXT NOT NULL,
            site_id TEXT NOT NULL,
            location_code TEXT NOT NULL,
            source_location_id TEXT,
            description TEXT,
            parent_location TEXT,
            status TEXT,
            classstructure_id TEXT,
            changed_at TEXT,
            source_snapshot_id TEXT NOT NULL,
            UNIQUE(source_schema, site_id, location_code, source_location_id)
        );
        CREATE INDEX IF NOT EXISTS idx_function_location_key ON function_location(source_schema, site_id, location_code);
        CREATE INDEX IF NOT EXISTS idx_function_location_desc ON function_location(description);

        CREATE TABLE IF NOT EXISTS location_hierarchy (
            hierarchy_record_id TEXT PRIMARY KEY,
            source_schema TEXT NOT NULL,
            site_id TEXT NOT NULL,
            location_code TEXT NOT NULL,
            parent_location TEXT,
            source_hierarchy_id TEXT,
            org_id TEXT,
            source_snapshot_id TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_location_hierarchy_key ON location_hierarchy(source_schema, site_id, location_code);

        CREATE TABLE IF NOT EXISTS device_specification (
            specification_record_id TEXT PRIMARY KEY,
            unified_device_id TEXT,
            source_schema TEXT NOT NULL,
            site_id TEXT,
            asset_number TEXT,
            source_spec_id TEXT,
            attribute_id TEXT,
            classstructure_id TEXT,
            alphanumeric_value TEXT,
            numeric_value TEXT,
            table_value TEXT,
            changed_at TEXT,
            source_snapshot_id TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_device_spec_device ON device_specification(unified_device_id);

        CREATE TABLE IF NOT EXISTS device_classification (
            classification_record_id TEXT PRIMARY KEY,
            source_schema TEXT NOT NULL,
            site_id TEXT,
            classstructure_id TEXT,
            classification_id TEXT,
            description TEXT,
            parent_classstructure_id TEXT,
            source_snapshot_id TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_device_classification_key ON device_classification(source_schema, site_id, classstructure_id);

        CREATE TABLE IF NOT EXISTS device_hierarchy (
            hierarchy_record_id TEXT PRIMARY KEY,
            unified_device_id TEXT,
            source_schema TEXT NOT NULL,
            site_id TEXT,
            asset_number TEXT,
            parent_asset_number TEXT,
            location_code TEXT,
            source_snapshot_id TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_device_hierarchy_device ON device_hierarchy(unified_device_id);

        CREATE TABLE IF NOT EXISTS device_context_link (
            link_id INTEGER PRIMARY KEY AUTOINCREMENT,
            unified_device_id TEXT NOT NULL,
            context_type TEXT NOT NULL,
            context_record_id TEXT NOT NULL,
            relation_method TEXT NOT NULL,
            confidence REAL NOT NULL,
            evidence_json TEXT NOT NULL,
            UNIQUE(unified_device_id, context_type, context_record_id)
        );
        CREATE INDEX IF NOT EXISTS idx_device_context_device ON device_context_link(unified_device_id, context_type);

        CREATE TABLE IF NOT EXISTS device_event (
            event_record_id TEXT PRIMARY KEY,
            unified_device_id TEXT,
            candidate_device_id TEXT,
            source_schema TEXT NOT NULL,
            event_type TEXT NOT NULL,
            source_table TEXT NOT NULL,
            source_row_id TEXT NOT NULL,
            site_id TEXT,
            location_code TEXT,
            event_time TEXT,
            status TEXT,
            description TEXT,
            source_snapshot_id TEXT NOT NULL,
            link_status TEXT NOT NULL,
            evidence_json TEXT NOT NULL,
            UNIQUE(source_schema, source_table, source_row_id)
        );
        CREATE INDEX IF NOT EXISTS idx_device_event_device_time ON device_event(unified_device_id, event_type, event_time);
        CREATE INDEX IF NOT EXISTS idx_device_event_source ON device_event(source_schema, source_table, source_row_id);

        DROP VIEW IF EXISTS v_latest_inspection;
        CREATE VIEW v_latest_inspection AS
        SELECT event_record_id, unified_device_id, source_schema, source_table, source_row_id,
               site_id, location_code, event_time, status, description, source_snapshot_id, evidence_json
          FROM (
                SELECT e.*, ROW_NUMBER() OVER (
                    PARTITION BY e.unified_device_id
                    ORDER BY COALESCE(e.event_time, '') DESC, e.event_record_id DESC
                ) AS rn
                  FROM device_event e
                 WHERE e.event_type='inspection' AND e.unified_device_id IS NOT NULL
               )
         WHERE rn=1;

        DROP VIEW IF EXISTS v_latest_inspection_candidate;
        CREATE VIEW v_latest_inspection_candidate AS
        SELECT event_record_id, candidate_device_id, source_schema, source_table, source_row_id,
               site_id, location_code, event_time, status, description, link_status, source_snapshot_id, evidence_json
          FROM (
                SELECT e.*, ROW_NUMBER() OVER (
                    PARTITION BY e.candidate_device_id
                    ORDER BY COALESCE(e.event_time, '') DESC, e.event_record_id DESC
                ) AS rn
                 FROM device_event e
                 WHERE e.event_type='inspection' AND e.candidate_device_id IS NOT NULL
                   AND e.link_status='needs_review'
               )
         WHERE rn=1;

        DROP VIEW IF EXISTS v_device_semantic_context;
        CREATE VIEW v_device_semantic_context AS
        SELECT u.unified_device_id, u.master_source_schema AS source_schema, u.site_id,
               u.asset_number, u.canonical_name, u.location_code AS asset_location_code,
               COALESCE(fl.description, '') AS location_description,
               COALESCE(fl.parent_location, '') AS parent_location,
               u.classstructure_id,
               li.event_record_id AS latest_inspection_id,
               li.event_time AS latest_inspection_time,
               li.status AS latest_inspection_status,
               li.description AS latest_inspection_result,
               lic.event_record_id AS latest_inspection_candidate_id,
               lic.event_time AS latest_inspection_candidate_time,
               lic.status AS latest_inspection_candidate_status,
               lic.description AS latest_inspection_candidate_result
          FROM unified_device u
          LEFT JOIN function_location fl
            ON fl.source_schema=u.master_source_schema
           AND fl.site_id=u.site_id
           AND fl.location_code=u.location_code
          LEFT JOIN v_latest_inspection li ON li.unified_device_id=u.unified_device_id
          LEFT JOIN v_latest_inspection_candidate lic ON lic.candidate_device_id=u.unified_device_id;
        """
    )


def asset_keys(connection: sqlite3.Connection) -> set[str]:
    return {f"{row[0]}\x1f{row[1]}\x1f{row[2]}" for row in connection.execute(
        "SELECT master_source_schema, site_id, location_code FROM unified_device WHERE location_code<>''"
    )}


def import_locations(connection: sqlite3.Connection, context_root: pathlib.Path, snapshot_id: str) -> dict[str, int]:
    needed = asset_keys(connection)
    counts: dict[str, int] = {}
    for path in sorted(context_root.glob("*_saas__context__locations.csv")):
        schema = path.name.split("__", 1)[0].upper()
        count = 0
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                site = clean(row.get("SITEID"))
                code = clean(row.get("LOCATION"))
                if not site or not code or f"{schema}\x1f{site}\x1f{code}" not in needed:
                    continue
                source_id = clean(row.get("LOCATIONSID"))
                record_id = sha_id("LOC-", schema, site, code, source_id)
                connection.execute(
                    """INSERT OR IGNORE INTO function_location
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (record_id, schema, site, code, source_id, clean(row.get("DESCRIPTION")), clean(row.get("PARENT")),
                     clean(row.get("STATUS")), clean(row.get("CLASSSTRUCTUREID")), clean(row.get("CHANGEDATE")), snapshot_id),
                )
                count += 1
        counts[schema] = count
        connection.commit()
    return counts


def import_hierarchy(connection: sqlite3.Connection, context_root: pathlib.Path, snapshot_id: str) -> int:
    needed = asset_keys(connection)
    total = 0
    for path in sorted(context_root.glob("*_saas__context__lochierarchy.csv")):
        schema = path.name.split("__", 1)[0].upper()
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                site = clean(row.get("SITEID"))
                code = clean(row.get("LOCATION"))
                if not site or not code or f"{schema}\x1f{site}\x1f{code}" not in needed:
                    continue
                source_id = clean(row.get("LOCHIERARCHYID"))
                record_id = sha_id("LH-", schema, site, code, source_id)
                connection.execute(
                    "INSERT OR IGNORE INTO location_hierarchy VALUES (?,?,?,?,?,?,?,?)",
                    (record_id, schema, site, code, clean(row.get("PARENT")), source_id, clean(row.get("ORGID")), snapshot_id),
                )
                total += 1
        connection.commit()
    return total


def import_specs_and_context(connection: sqlite3.Connection, context_root: pathlib.Path, snapshot_id: str) -> dict[str, int]:
    total: dict[str, int] = {}
    for path in sorted(context_root.glob("*_saas__context__assetspec.csv")):
        schema = path.name.split("__", 1)[0].upper()
        count = 0
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                site = clean(row.get("SITEID")); asset = clean(row.get("ASSETNUM"))
                if not site or not asset:
                    continue
                device = connection.execute(
                    "SELECT unified_device_id FROM unified_device WHERE master_source_schema=? AND site_id=? AND asset_number=?",
                    (schema, site, asset),
                ).fetchone()
                record_id = sha_id("SPEC-", schema, site, asset, clean(row.get("ASSETSPECID")), clean(row.get("ASSETATTRID")))
                connection.execute(
                    """INSERT OR IGNORE INTO device_specification VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (record_id, device[0] if device else None, schema, site, asset, clean(row.get("ASSETSPECID")),
                     clean(row.get("ASSETATTRID")), clean(row.get("CLASSSTRUCTUREID")), clean(row.get("ALNVALUE")),
                     clean(row.get("NUMVALUE")), clean(row.get("TABLEVALUE")), clean(row.get("CHANGEDATE")), snapshot_id),
                )
                count += 1
        total[schema] = count
        connection.commit()
    for path in sorted(context_root.glob("*_saas__context__classstructure.csv")):
        schema = path.name.split("__", 1)[0].upper()
        count = 0
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                class_id = clean(row.get("CLASSSTRUCTUREID"))
                if not class_id:
                    continue
                record_id = sha_id("CLS-", schema, clean(row.get("SITEID")), class_id)
                connection.execute(
                    """INSERT OR IGNORE INTO device_classification VALUES (?,?,?,?,?,?,?,?)""",
                    (record_id, schema, clean(row.get("SITEID")), class_id, clean(row.get("CLASSIFICATIONID")),
                     clean(row.get("DESCRIPTION")) or clean(row.get("DESCRIPTION_CLASS")) or clean(row.get("GENASSETDESC")),
                     clean(row.get("PARENT")), snapshot_id),
                )
                count += 1
        total[f"{schema}:classstructure"] = count
        connection.commit()
    for path in sorted(context_root.glob("*_saas__context__assethierarchy.csv")):
        schema = path.name.split("__", 1)[0].upper()
        count = 0
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                site = clean(row.get("SITEID")); asset = clean(row.get("ASSETNUM"))
                if not site or not asset:
                    continue
                device = connection.execute(
                    "SELECT unified_device_id FROM unified_device WHERE master_source_schema=? AND site_id=? AND asset_number=?",
                    (schema, site, asset),
                ).fetchone()
                record_id = sha_id("AH-", schema, site, asset, clean(row.get("ASSETHIERARCHYID")))
                connection.execute(
                    "INSERT OR IGNORE INTO device_hierarchy VALUES (?,?,?,?,?,?,?,?)",
                    (record_id, device[0] if device else None, schema, site, asset, clean(row.get("PARENT")), clean(row.get("LOCATION")), snapshot_id),
                )
                count += 1
        total[f"{schema}:assethierarchy"] = count
        connection.commit()
    return total


def create_context_links(connection: sqlite3.Connection) -> dict[str, int]:
    connection.execute(
        """INSERT OR IGNORE INTO device_context_link
           (unified_device_id, context_type, context_record_id, relation_method, confidence, evidence_json)
           SELECT u.unified_device_id, 'function_location', f.location_record_id, 'asset.location_exact', 1.0,
                  json_object('site_id', u.site_id, 'location_code', u.location_code)
             FROM unified_device u JOIN function_location f
               ON f.source_schema=u.master_source_schema AND f.site_id=u.site_id AND f.location_code=u.location_code
            WHERE u.location_code<>''"""
    )
    connection.execute(
        """INSERT OR IGNORE INTO device_context_link
           (unified_device_id, context_type, context_record_id, relation_method, confidence, evidence_json)
           SELECT u.unified_device_id, 'location_hierarchy', h.hierarchy_record_id, 'asset.location_hierarchy', 0.98,
                  json_object('site_id', u.site_id, 'location_code', u.location_code, 'parent_location', h.parent_location)
             FROM unified_device u JOIN location_hierarchy h
               ON h.source_schema=u.master_source_schema AND h.site_id=u.site_id AND h.location_code=u.location_code
            WHERE u.location_code<>''"""
    )
    connection.commit()
    return {row[0]: row[1] for row in connection.execute("SELECT context_type, COUNT(*) FROM device_context_link GROUP BY context_type")}


def first_event_time(row: dict[str, str]) -> str:
    for field in ("INSPECTION_DATE", "CHECKDATE", "CHECKTIME", "REPORTDATE", "C_FAULTDATE", "CREATEDATE", "C_CREATEDATE", "CHANGEDATE"):
        if clean(row.get(field)):
            return clean(row.get(field))
    return ""


def import_events(connection: sqlite3.Connection, operational_root: pathlib.Path, snapshot_id: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for schema, group, table, ordinal, row in read_operational_files(operational_root):
        source_row_id = event_row_id(row, ordinal)
        event_record_id = sha_id("EV-", schema, table, source_row_id)
        mapping = connection.execute(
            """SELECT candidate_device_id, candidate_device_id, decision, match_method, evidence_json
                 FROM device_match_candidate
                WHERE source_schema=? AND source_table_group=? AND source_table=? AND source_row_id=?""",
            (schema, group, table, source_row_id),
        ).fetchone()
        candidate_id, unified_id, decision, method, evidence = mapping or (None, None, "unmatched", "not_in_candidate", "{}")
        site = clean(row.get("SITEID")) or clean(row.get("ASSETSITEID"))
        location = clean(row.get("LOCATION")) or clean(row.get("WORKLOCATION"))
        event_type = group
        description = event_description(row)
        connection.execute(
            """INSERT OR IGNORE INTO device_event
               (event_record_id, unified_device_id, candidate_device_id, source_schema, event_type, source_table,
                source_row_id, site_id, location_code, event_time, status, description, source_snapshot_id,
                link_status, evidence_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (event_record_id, unified_id if decision == "accepted" else None, str(candidate_id) if candidate_id else None,
             schema, event_type, table, source_row_id, site, location, first_event_time(row), clean(row.get("STATUS")),
             description, snapshot_id, "accepted" if decision == "accepted" else decision,
             json.dumps({"candidate_device_id": candidate_id, "match_method": method, "candidate_evidence": evidence}, ensure_ascii=False)),
        )
        counts[f"{event_type}:{'linked' if decision == 'accepted' else decision}"] = counts.get(f"{event_type}:{'linked' if decision == 'accepted' else decision}", 0) + 1
    connection.commit()
    return counts


def main() -> None:
    result_root_value = os.environ.get("IDENTITY_RESULT_ROOT")
    context_root_value = os.environ.get("IDENTITY_CONTEXT_SNAPSHOT")
    operational_root_value = os.environ.get("IDENTITY_OPERATIONAL_SNAPSHOT")
    result_root = pathlib.Path(result_root_value) if result_root_value else sorted((ROOT / "results").glob("identity-layer-v1-*/"), reverse=True)[0]
    context_root = pathlib.Path(context_root_value) if context_root_value else latest_snapshot("identity-context-snapshot-*/")
    operational_root = pathlib.Path(operational_root_value) if operational_root_value else latest_snapshot("identity-operational-snapshot-*/")
    for value_name, value in (("result", result_root), ("context", context_root), ("operational", operational_root)):
        if not value.is_absolute():
            value = (REPO_ROOT / value).resolve()
        if value_name == "result": result_root = value
        elif value_name == "context": context_root = value
        else: operational_root = value
    db_path = result_root / "identity_semantics.sqlite3"
    connection = sqlite3.connect(db_path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    make_schema(connection)
    locations = {} if connection.execute("SELECT COUNT(*) FROM function_location").fetchone()[0] else import_locations(connection, context_root, context_root.name)
    hierarchy = 0 if connection.execute("SELECT COUNT(*) FROM location_hierarchy").fetchone()[0] else import_hierarchy(connection, context_root, context_root.name)
    context_counts = {} if connection.execute("SELECT COUNT(*) FROM device_specification").fetchone()[0] else import_specs_and_context(connection, context_root, context_root.name)
    links = (
        {row[0]: row[1] for row in connection.execute("SELECT context_type, COUNT(*) FROM device_context_link GROUP BY context_type")}
        if connection.execute("SELECT COUNT(*) FROM device_context_link").fetchone()[0]
        else create_context_links(connection)
    )
    event_counts = import_events(connection, operational_root, operational_root.name)
    row = connection.execute(
        "SELECT COUNT(*), COUNT(latest_inspection_id) FROM v_device_semantic_context"
    ).fetchone()
    connection.commit()
    connection.close()
    report = {
        "result_root": str(result_root),
        "context_snapshot_id": context_root.name,
        "operational_snapshot_id": operational_root.name,
        "location_rows_referenced_by_devices": locations,
        "location_hierarchy_rows_referenced_by_devices": hierarchy,
        "specification_and_classification_rows": context_counts,
        "device_context_links": links,
        "event_rows": event_counts,
        "semantic_context_devices": row[0],
        "devices_with_latest_inspection": row[1],
        "source_write": False,
        "formal_publication": False,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    (result_root / "context_layer_manifest.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
