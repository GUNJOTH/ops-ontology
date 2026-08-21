"""Build the local relational business-semantic overlay.

The large identity snapshot remains read-only.  This script creates a small
local SQLite overlay containing concepts, source-record links and explicit
device relations.  It never changes the source snapshot or the source EAM
tables.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import sqlite3
from datetime import datetime, timezone


ROOT = pathlib.Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parent
IDENTITY_RESULTS = PROJECT_ROOT / "pilots" / "identity" / "results"
DEFAULT_OVERLAY = ROOT / "data" / "unified_semantics.sqlite3"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def stable_id(prefix: str, *parts: object) -> str:
    raw = "|".join("" if value is None else str(value) for value in parts)
    return f"{prefix}-{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:24]}"


def latest_identity_db() -> pathlib.Path:
    for directory in sorted(IDENTITY_RESULTS.glob("identity-layer-v1-*/"), reverse=True):
        manifest = directory / "manifest.json"
        database = directory / "identity_semantics.sqlite3"
        if manifest.exists() and database.exists():
            return database
    raise SystemExit("未找到可用的身份结果库")


def init_overlay(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        PRAGMA foreign_keys=ON;
        CREATE TABLE IF NOT EXISTS semantic_concept (
          concept_key TEXT PRIMARY KEY,
          concept_type TEXT NOT NULL CHECK (concept_type IN ('device','location','inspection','defect','work_order','relation','source_system')),
          canonical_name TEXT NOT NULL,
          description TEXT NOT NULL DEFAULT '',
          source_scope TEXT NOT NULL,
          version TEXT NOT NULL,
          status TEXT NOT NULL CHECK (status IN ('active','draft','blocked')),
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS source_system (
          system_key TEXT PRIMARY KEY,
          display_name TEXT NOT NULL,
          connection_kind TEXT NOT NULL,
          snapshot_id TEXT NOT NULL,
          read_only INTEGER NOT NULL CHECK (read_only=1),
          status TEXT NOT NULL CHECK (status IN ('active','planned','blocked')),
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS unified_device_relation (
          relation_id TEXT PRIMARY KEY,
          subject_unified_device_id TEXT NOT NULL,
          predicate TEXT NOT NULL CHECK (predicate IN ('parent_device','same_function_location','related_device')),
          object_unified_device_id TEXT,
          source_schema TEXT NOT NULL,
          source_table TEXT NOT NULL,
          source_row_id TEXT NOT NULL,
          confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
          status TEXT NOT NULL CHECK (status IN ('accepted','needs_review','blocked')),
          evidence_json TEXT NOT NULL,
          source_snapshot_id TEXT NOT NULL,
          created_at TEXT NOT NULL,
          UNIQUE(subject_unified_device_id,predicate,object_unified_device_id,source_schema,source_row_id)
        );
        CREATE TABLE IF NOT EXISTS business_record_link (
          link_id TEXT PRIMARY KEY,
          unified_device_id TEXT,
          source_schema TEXT NOT NULL,
          source_table_group TEXT NOT NULL,
          source_table TEXT NOT NULL,
          source_row_id TEXT NOT NULL,
          business_type TEXT NOT NULL CHECK (business_type IN ('inspection','defect','work_order')),
          source_key_type TEXT NOT NULL,
          source_key TEXT NOT NULL,
          status TEXT NOT NULL CHECK (status IN ('accepted','needs_review','blocked')),
          confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
          evidence_json TEXT NOT NULL,
          source_snapshot_id TEXT NOT NULL,
          created_at TEXT NOT NULL,
          UNIQUE(source_schema,source_table_group,source_table,source_row_id)
        );
        CREATE TABLE IF NOT EXISTS semantic_layer_run (
          run_id TEXT PRIMARY KEY,
          identity_db_path TEXT NOT NULL,
          source_snapshot_id TEXT NOT NULL,
          unified_device_count INTEGER NOT NULL,
          identity_map_count INTEGER NOT NULL,
          relation_count INTEGER NOT NULL,
          business_link_count INTEGER NOT NULL,
          accepted_business_link_count INTEGER NOT NULL,
          source_write INTEGER NOT NULL CHECK (source_write=0),
          formal_publication INTEGER NOT NULL CHECK (formal_publication=0),
          created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_unified_relation_subject ON unified_device_relation(subject_unified_device_id,status);
        CREATE INDEX IF NOT EXISTS ix_unified_relation_object ON unified_device_relation(object_unified_device_id,status);
        CREATE INDEX IF NOT EXISTS ix_business_link_device ON business_record_link(unified_device_id,status);
        CREATE INDEX IF NOT EXISTS ix_business_link_source ON business_record_link(source_schema,source_table_group,source_table,status);
        """
    )


def build(identity_db: pathlib.Path, overlay_db: pathlib.Path) -> dict[str, object]:
    overlay_db.parent.mkdir(parents=True, exist_ok=True)
    identity = sqlite3.connect(f"file:{identity_db.resolve()}?mode=ro", uri=True, timeout=30)
    identity.row_factory = sqlite3.Row
    target = sqlite3.connect(str(overlay_db), timeout=30)
    target.row_factory = sqlite3.Row
    target.execute("PRAGMA journal_mode=WAL")
    target.execute("PRAGMA foreign_keys=ON")
    init_overlay(target)

    now = utc_now()
    identity_run = identity.execute(
        "SELECT asset_snapshot_id FROM identity_run ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    snapshot_id = str(identity_run["asset_snapshot_id"] if identity_run else identity_db.parent.name)
    source_scope = "HD_SAAS,XNY_SAAS"
    concepts = [
        ("device", "device", "统一设备", "跨源设备身份的本地业务对象"),
        ("location", "location", "功能位置", "设备所在的功能位置上下文"),
        ("inspection", "inspection", "巡检记录", "巡检系统中的设备业务记录"),
        ("defect", "defect", "缺陷记录", "缺陷系统中的设备业务记录"),
        ("work_order", "work_order", "工单", "工单系统中的设备业务记录"),
        ("relation", "relation", "设备关系", "设备父子和显式关联关系"),
        ("source_system", "source_system", "来源系统", "业务数据来源系统"),
    ]
    target.executemany(
        """INSERT OR IGNORE INTO semantic_concept
           (concept_key,concept_type,canonical_name,description,source_scope,version,status,created_at)
           VALUES (?,?,?,?,?,'unified-device-semantics-v1','active',?)""",
        [(key, kind, name, description, source_scope, now) for key, kind, name, description in concepts],
    )
    source_systems = [
        ("HD_SAAS", "HD_SAAS", "read_only_snapshot", snapshot_id, 1, "active", now),
        ("XNY_SAAS", "XNY_SAAS", "read_only_snapshot", snapshot_id, 1, "active", now),
    ]
    target.executemany(
        """INSERT OR IGNORE INTO source_system
           (system_key,display_name,connection_kind,snapshot_id,read_only,status,created_at)
           VALUES (?,?,?,?,?,?,?)""",
        source_systems,
    )

    parent_rows = identity.execute(
        """
        SELECT child.unified_device_id, child.master_source_schema, child.site_id,
               child.asset_number, child.parent_asset_number,
               parent.unified_device_id AS parent_unified_device_id
        FROM unified_device child
        LEFT JOIN unified_device parent
          ON parent.master_source_schema=child.master_source_schema
         AND parent.site_id=child.site_id
         AND parent.asset_number=child.parent_asset_number
        WHERE trim(coalesce(child.parent_asset_number,''))<>''
        """
    ).fetchall()
    relations = []
    for row in parent_rows:
        object_id = row["parent_unified_device_id"]
        status = "accepted" if object_id else "needs_review"
        confidence = 1.0 if object_id else 0.0
        evidence = {
            "source": "unified_device.parent_asset_number",
            "site_id": row["site_id"],
            "parent_asset_number": row["parent_asset_number"],
            "same_source_schema": True,
        }
        relations.append((
            stable_id("REL", row["unified_device_id"], "parent_device", object_id, row["master_source_schema"], row["asset_number"]),
            row["unified_device_id"], "parent_device", object_id, row["master_source_schema"],
            "ASSET", f"ASSETNUM:{row['asset_number']}", confidence, status,
            json.dumps(evidence, ensure_ascii=False, sort_keys=True), snapshot_id, now,
        ))
    target.executemany(
        """INSERT OR IGNORE INTO unified_device_relation
           (relation_id,subject_unified_device_id,predicate,object_unified_device_id,
            source_schema,source_table,source_row_id,confidence,status,evidence_json,
            source_snapshot_id,created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        relations,
    )

    map_rows = identity.execute(
        """
        SELECT unified_device_id,source_schema,source_table_group,source_table,source_row_id,
               source_key_type,source_key,status,match_confidence,evidence_json,source_snapshot_id
        FROM device_identity_map
        WHERE source_table_group IN ('inspection','defect','work_order')
        """
    ).fetchall()
    links = []
    for row in map_rows:
        business_type = row["source_table_group"]
        links.append((
            stable_id("LINK", row["source_schema"], row["source_table_group"], row["source_table"], row["source_row_id"]),
            row["unified_device_id"], row["source_schema"], row["source_table_group"], row["source_table"],
            row["source_row_id"], business_type, row["source_key_type"], row["source_key"], row["status"],
            float(row["match_confidence"] or 0), row["evidence_json"] or "{}", row["source_snapshot_id"], now,
        ))
    target.executemany(
        """INSERT OR IGNORE INTO business_record_link
           (link_id,unified_device_id,source_schema,source_table_group,source_table,source_row_id,
            business_type,source_key_type,source_key,status,confidence,evidence_json,source_snapshot_id,created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        links,
    )
    device_count = int(identity.execute("SELECT count(*) FROM unified_device").fetchone()[0])
    identity_map_count = int(identity.execute("SELECT count(*) FROM device_identity_map").fetchone()[0])
    accepted_links = sum(1 for row in links if row[9] == "accepted")
    run_id = stable_id("SEM", snapshot_id, identity_db, now)
    target.execute(
        """INSERT INTO semantic_layer_run
           (run_id,identity_db_path,source_snapshot_id,unified_device_count,identity_map_count,
            relation_count,business_link_count,accepted_business_link_count,source_write,formal_publication,created_at)
           VALUES (?,?,?,?,?,?,?,?,0,0,?)""",
        (run_id, str(identity_db.resolve()), snapshot_id, device_count, identity_map_count,
         len(relations), len(links), accepted_links, now),
    )
    target.commit()
    target.close()
    identity.close()
    return {
        "run_id": run_id,
        "overlay_db": str(overlay_db.resolve()),
        "identity_db": str(identity_db.resolve()),
        "source_snapshot_id": snapshot_id,
        "unified_device_count": device_count,
        "identity_map_count": identity_map_count,
        "relation_count": len(relations),
        "business_link_count": len(links),
        "accepted_business_link_count": accepted_links,
        "source_write": False,
        "formal_publication": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the local relational unified-device semantic overlay")
    parser.add_argument("--identity-db", type=pathlib.Path, default=None)
    parser.add_argument("--overlay-db", type=pathlib.Path, default=DEFAULT_OVERLAY)
    args = parser.parse_args()
    identity_db = (args.identity_db or latest_identity_db()).resolve()
    if not identity_db.exists():
        raise SystemExit(f"身份结果库不存在: {identity_db}")
    print(json.dumps(build(identity_db, args.overlay_db.resolve()), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
