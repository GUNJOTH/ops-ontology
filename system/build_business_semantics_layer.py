"""Build the relational business-object and knowledge-identity layer.

This is a local metadata/relationship layer.  It keeps source workflow and
identity databases read-only, stores stable knowledge identities and versions,
and keeps source references instead of copying business documents or events.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone

from pipeline.contracts import connect_readonly
from semantic_registry import canonical_relation_key, object_class_local_name

ROOT = pathlib.Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parent
DATA_DIR = ROOT / "data"
DEFAULT_TARGET = DATA_DIR / "unified_semantics.sqlite3"
DEFAULT_WORKFLOW = DATA_DIR / "semantic_workflow.sqlite3"
IDENTITY_RESULTS = PROJECT_ROOT / "pilots" / "identity" / "results"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def stable_id(prefix: str, *parts: object) -> str:
    raw = "|".join("" if value is None else str(value) for value in parts)
    return f"{prefix}-{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:24]}"


def latest_identity_db() -> pathlib.Path:
    for directory in sorted(IDENTITY_RESULTS.glob("identity-layer-v1-*/"), reverse=True):
        database = directory / "identity_semantics.sqlite3"
        if (directory / "manifest.json").exists() and database.exists():
            return database
    raise SystemExit("未找到可用的身份结果库")


def parse_json(value: str | None) -> dict[str, object]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else {"value": parsed}
    except (TypeError, json.JSONDecodeError):
        return {"raw": value}


def init_layer(target: sqlite3.Connection) -> None:
    target.executescript(
        """
        PRAGMA foreign_keys=ON;
        CREATE TABLE IF NOT EXISTS business_object_type (
          object_type TEXT PRIMARY KEY,
          display_name TEXT NOT NULL,
          description TEXT NOT NULL,
          rdf_class TEXT,
          parent_object_type TEXT,
          version TEXT NOT NULL,
          status TEXT NOT NULL CHECK (status IN ('active','draft','blocked')),
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS knowledge_asset (
          asset_id TEXT PRIMARY KEY,
          asset_key TEXT NOT NULL UNIQUE,
          asset_type TEXT NOT NULL CHECK (asset_type IN ('rule','terminology','evaluation_case','definition')),
          title TEXT NOT NULL,
          canonical_definition TEXT NOT NULL DEFAULT '',
          current_version TEXT NOT NULL,
          status TEXT NOT NULL CHECK (status IN ('registered','draft','proposed','replayed','approved','enabled','retired','blocked','needs_review')),
          source_scope TEXT NOT NULL,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS knowledge_asset_version (
          asset_version_id TEXT PRIMARY KEY,
          asset_id TEXT NOT NULL REFERENCES knowledge_asset(asset_id),
          version TEXT NOT NULL,
          content_hash TEXT NOT NULL,
          definition_json TEXT NOT NULL,
          status TEXT NOT NULL,
          preview_count INTEGER NOT NULL DEFAULT 0,
          replay_count INTEGER NOT NULL DEFAULT 0,
          replay_pass_count INTEGER NOT NULL DEFAULT 0,
          replay_fail_count INTEGER NOT NULL DEFAULT 0,
          source_workflow_id TEXT,
          replay_id TEXT,
          created_at TEXT NOT NULL,
          UNIQUE(asset_id,version)
        );
        CREATE TABLE IF NOT EXISTS knowledge_asset_part (
          part_id TEXT PRIMARY KEY,
          asset_version_id TEXT NOT NULL REFERENCES knowledge_asset_version(asset_version_id),
          part_type TEXT NOT NULL CHECK (part_type IN ('condition','rule','action','evidence')),
          ordinal INTEGER NOT NULL,
          label TEXT NOT NULL,
          expression_json TEXT NOT NULL,
          created_at TEXT NOT NULL,
          UNIQUE(asset_version_id,part_type,ordinal)
        );
        CREATE TABLE IF NOT EXISTS knowledge_asset_source (
          source_id TEXT PRIMARY KEY,
          asset_id TEXT NOT NULL REFERENCES knowledge_asset(asset_id),
          source_kind TEXT NOT NULL,
          source_record_id TEXT NOT NULL,
          source_table TEXT NOT NULL,
          source_snapshot_id TEXT,
          source_status TEXT,
          evidence_json TEXT NOT NULL,
          created_at TEXT NOT NULL,
          UNIQUE(asset_id,source_kind,source_record_id)
        );
        CREATE TABLE IF NOT EXISTS knowledge_asset_binding (
          binding_id TEXT PRIMARY KEY,
          asset_id TEXT NOT NULL REFERENCES knowledge_asset(asset_id),
          object_type TEXT NOT NULL,
          object_key TEXT NOT NULL,
          relation_type TEXT NOT NULL,
          status TEXT NOT NULL CHECK (status IN ('accepted','needs_review','blocked')),
          evidence_json TEXT NOT NULL,
          created_at TEXT NOT NULL,
          UNIQUE(asset_id,object_type,object_key,relation_type)
        );
        CREATE TABLE IF NOT EXISTS knowledge_asset_issue (
          issue_id TEXT PRIMARY KEY,
          asset_id TEXT NOT NULL REFERENCES knowledge_asset(asset_id),
          issue_type TEXT NOT NULL CHECK (issue_type IN ('duplicate','difference','conflict','missing_evidence')),
          severity TEXT NOT NULL CHECK (severity IN ('low','medium','high')),
          status TEXT NOT NULL CHECK (status IN ('open','resolved','ignored')),
          details_json TEXT NOT NULL,
          created_at TEXT NOT NULL,
          resolved_at TEXT
        );
        CREATE TABLE IF NOT EXISTS knowledge_layer_run (
          run_id TEXT PRIMARY KEY,
          workflow_db_path TEXT NOT NULL,
          identity_db_path TEXT NOT NULL,
          asset_count INTEGER NOT NULL,
          version_count INTEGER NOT NULL,
          source_count INTEGER NOT NULL,
          binding_count INTEGER NOT NULL,
          issue_count INTEGER NOT NULL,
          source_write INTEGER NOT NULL CHECK (source_write=0),
          formal_publication INTEGER NOT NULL CHECK (formal_publication=0),
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS business_object_relation (
          relation_id TEXT PRIMARY KEY,
          subject_type TEXT NOT NULL,
          subject_key TEXT NOT NULL,
          predicate TEXT NOT NULL,
          object_type TEXT NOT NULL,
          object_key TEXT,
          source_schema TEXT NOT NULL,
          source_table TEXT NOT NULL,
          source_row_id TEXT NOT NULL,
          status TEXT NOT NULL CHECK (status IN ('accepted','needs_review','blocked')),
          confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
          evidence_json TEXT NOT NULL,
          source_snapshot_id TEXT,
          created_at TEXT NOT NULL,
          UNIQUE(subject_type,subject_key,predicate,object_type,object_key,source_schema,source_table,source_row_id)
        );
        CREATE INDEX IF NOT EXISTS ix_knowledge_asset_status ON knowledge_asset(asset_type,status);
        CREATE INDEX IF NOT EXISTS ix_knowledge_asset_version_asset ON knowledge_asset_version(asset_id,version);
        CREATE INDEX IF NOT EXISTS ix_knowledge_asset_source_asset ON knowledge_asset_source(asset_id,source_kind);
        CREATE INDEX IF NOT EXISTS ix_knowledge_asset_binding_object ON knowledge_asset_binding(object_type,object_key,status);
        CREATE INDEX IF NOT EXISTS ix_knowledge_asset_issue_status ON knowledge_asset_issue(status,issue_type);
        CREATE INDEX IF NOT EXISTS ix_business_object_relation_subject ON business_object_relation(subject_type,subject_key,status);
        CREATE INDEX IF NOT EXISTS ix_business_object_relation_object ON business_object_relation(object_type,object_key,status);
        """
    )
    columns = {row[1] for row in target.execute("PRAGMA table_info(business_object_type)").fetchall()}
    if "parent_object_type" not in columns:
        target.execute("ALTER TABLE business_object_type ADD COLUMN parent_object_type TEXT")
    if "rdf_class" not in columns:
        target.execute("ALTER TABLE business_object_type ADD COLUMN rdf_class TEXT")


def upsert_asset(target: sqlite3.Connection, asset_key: str, asset_type: str, title: str,
                 definition: dict[str, object], version: str, status: str, scope: str,
                 now: str) -> str:
    asset_id = stable_id("KA", asset_key)
    target.execute(
        """
        INSERT INTO knowledge_asset(asset_id,asset_key,asset_type,title,canonical_definition,
          current_version,status,source_scope,created_at,updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(asset_key) DO UPDATE SET title=excluded.title,
          canonical_definition=excluded.canonical_definition,current_version=excluded.current_version,
          status=excluded.status,updated_at=excluded.updated_at
        """,
        (asset_id, asset_key, asset_type, title, str(definition.get("canonical", "")), version, status, scope, now, now),
    )
    return asset_id


def add_version(target: sqlite3.Connection, asset_id: str, version: str, definition: dict[str, object],
                status: str, source_workflow_id: str | None, replay_id: str | None,
                preview_count: int, replay_count: int, replay_pass_count: int,
                replay_fail_count: int, now: str) -> str:
    definition_json = json.dumps(definition, ensure_ascii=False, sort_keys=True)
    content_hash = hashlib.sha256(definition_json.encode("utf-8")).hexdigest()
    version_id = stable_id("KAV", asset_id, version)
    target.execute(
        """
        INSERT INTO knowledge_asset_version(asset_version_id,asset_id,version,content_hash,definition_json,
          status,preview_count,replay_count,replay_pass_count,replay_fail_count,source_workflow_id,replay_id,created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(asset_id,version) DO UPDATE SET definition_json=excluded.definition_json,
          content_hash=excluded.content_hash,status=excluded.status,preview_count=excluded.preview_count,
          replay_count=excluded.replay_count,replay_pass_count=excluded.replay_pass_count,
          replay_fail_count=excluded.replay_fail_count,source_workflow_id=excluded.source_workflow_id,
          replay_id=excluded.replay_id
        """,
        (version_id, asset_id, version, content_hash, definition_json, status, preview_count, replay_count,
         replay_pass_count, replay_fail_count, source_workflow_id, replay_id, now),
    )
    return version_id


def add_part(target: sqlite3.Connection, version_id: str, part_type: str, ordinal: int,
             label: str, expression: object, now: str) -> None:
    target.execute(
        """
        INSERT OR IGNORE INTO knowledge_asset_part(part_id,asset_version_id,part_type,ordinal,label,expression_json,created_at)
        VALUES (?,?,?,?,?,?,?)
        """,
        (stable_id("KAP", version_id, part_type, ordinal), version_id, part_type, ordinal, label,
         json.dumps(expression, ensure_ascii=False, sort_keys=True), now),
    )


def add_source(target: sqlite3.Connection, asset_id: str, source_kind: str, source_record_id: str,
               source_table: str, snapshot_id: str | None, source_status: str | None,
               evidence: dict[str, object], now: str) -> None:
    target.execute(
        """
        INSERT OR IGNORE INTO knowledge_asset_source(source_id,asset_id,source_kind,source_record_id,
          source_table,source_snapshot_id,source_status,evidence_json,created_at)
        VALUES (?,?,?,?,?,?,?,?,?)
        """,
        (stable_id("KAS", asset_id, source_kind, source_record_id), asset_id, source_kind, source_record_id,
         source_table, snapshot_id, source_status, json.dumps(evidence, ensure_ascii=False, sort_keys=True), now),
    )


def build(workflow_db: pathlib.Path, identity_db: pathlib.Path, target_db: pathlib.Path) -> dict[str, object]:
    target_db.parent.mkdir(parents=True, exist_ok=True)
    workflow = connect_readonly(workflow_db, timeout=30)
    identity = connect_readonly(identity_db, timeout=30)
    target = sqlite3.connect(str(target_db), timeout=30)
    target.row_factory = sqlite3.Row
    target.execute("PRAGMA foreign_keys=ON")
    init_layer(target)
    now = utc_now()
    workflow_snapshot = f"workflow-local:{workflow_db.name}"

    object_types = [
        ("organization", "组织", "组织业务对象根类型", None, "business-object-v1"),
        ("company", "分公司", "组织下的分公司", "organization", "business-object-v1"),
        ("center", "中心", "分公司下的中心", "company", "business-object-v1"),
        ("team", "班组", "中心下的班组", "center", "business-object-v1"),
        ("physical_object", "物理对象", "站点和设备的物理对象根类型", None, "business-object-v1"),
        ("site", "站点", "设备运行所在站点", "physical_object", "business-object-v1"),
        ("device", "设备", "设备统一业务对象，由 source_schema + SITEID + ASSETNUM 定位", "physical_object", "unified-device-v1"),
        ("location", "位置", "设备所在的功能位置和位置层级", "physical_object", "unified-location-v1"),
        ("business_event", "业务事件", "巡检、缺陷、消缺和工单的事件根类型", None, "business-object-v1"),
        ("inspection", "巡检", "巡检事实记录，不复制业务明细", "business_event", "business-fact-v1"),
        ("observation_event", "观测事件", "设备测点或观测结果事件，必须有来源字段和时间证据", "business_event", "business-fact-v1"),
        ("abnormal_inspection", "异常巡检", "巡检中的异常事件类型，需有来源字段证据", "inspection", "business-fact-v1"),
        ("defect", "缺陷", "缺陷事实记录，不复制业务明细", "business_event", "business-fact-v1"),
        ("defect_event", "缺陷事件", "缺陷生命周期中的事件类型", "business_event", "business-fact-v1"),
        ("defect_resolution", "消缺", "缺陷处理/消缺事实记录，不复制业务明细", "business_event", "business-fact-v1"),
        ("resolution_event", "消缺事件", "消缺动作或处理完成事件", "business_event", "business-fact-v1"),
        ("work_order", "工单", "工单事实记录，不复制业务明细", "business_event", "business-fact-v1"),
        ("work_order_event", "工单事件", "工单状态和执行事件", "business_event", "business-fact-v1"),
        ("escalation_event", "升级事件", "规则判断产生的升级处理事件", "business_event", "business-fact-v1"),
        ("business_knowledge", "业务知识", "标准、规则、SOP 和风险的知识根类型", None, "knowledge-object-v1"),
        ("standard", "标准", "业务标准知识资产类型", "business_knowledge", "knowledge-object-v1"),
        ("rule", "规则", "可判断、可计算、可约束、可行动的规则知识", "business_knowledge", "knowledge-object-v1"),
        ("sop", "SOP", "标准作业程序知识资产类型", "business_knowledge", "knowledge-object-v1"),
        ("risk", "风险", "风险知识资产类型", "business_knowledge", "knowledge-object-v1"),
        ("knowledge_asset", "知识资产", "有身份、来源、版本和审核状态的知识对象", "business_knowledge", "knowledge-asset-v1"),
        ("rule_decision", "规则判断", "规则对业务事实的可解释判断结果", "business_knowledge", "machine-semantics-v1"),
        ("semantic_fact", "派生事实", "由来源事实和规则推导出的新事实", "business_knowledge", "machine-semantics-v1"),
    ]
    target.executemany(
        """INSERT INTO business_object_type(object_type,display_name,description,rdf_class,parent_object_type,version,status,created_at)
           VALUES (?,?,?,?,?,?, 'active',?)
           ON CONFLICT(object_type) DO UPDATE SET display_name=excluded.display_name,
             description=excluded.description,rdf_class=excluded.rdf_class,parent_object_type=excluded.parent_object_type,version=excluded.version""",
        [(key, name, description, object_class_local_name(key), parent, version, now) for key, name, description, parent, version in object_types],
    )

    asset_by_rule: dict[str, str] = {}
    source_count = 0
    version_count = 0
    binding_count = 0
    issue_count = 0

    registry_rows = workflow.execute("SELECT * FROM cleaning_rule_registry ORDER BY rule_key").fetchall()
    proposal_rows = workflow.execute("SELECT * FROM rule_agent_proposal ORDER BY created_at,proposal_id").fetchall()
    proposal_by_rule: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in proposal_rows:
        proposal_by_rule[row["rule_key"]].append(row)

    for row in registry_rows:
        rule_key = row["rule_key"]
        asset_key = f"RULE:{rule_key}"
        status = "enabled" if int(row["enabled"] or 0) else "registered"
        definition = {
            "canonical": row["rule_label"] or rule_key,
            "rule_key": rule_key,
            "cleaning_type": row["cleaning_type"],
            "action": row["action_label"],
            "metadata": parse_json(row["metadata_json"]),
        }
        asset_id = upsert_asset(target, asset_key, "rule", row["rule_label"] or rule_key, definition,
                                row["rule_version"] or "unversioned", status, "HD_SAAS,XNY_SAAS", now)
        asset_by_rule[rule_key] = asset_id
        version_id = add_version(target, asset_id, row["rule_version"] or "unversioned", definition, status,
                                 rule_key, row["replay_id"], 0, 0, 0, 0, now)
        add_part(target, version_id, "rule", 1, rule_key, {"cleaning_type": row["cleaning_type"]}, now)
        add_part(target, version_id, "action", 1, row["action_label"] or "", {"label": row["action_label"] or ""}, now)
        add_source(target, asset_id, "cleaning_rule_registry", rule_key, "cleaning_rule_registry", workflow_snapshot,
                   status, {"rule_version": row["rule_version"], "replay_id": row["replay_id"]}, now)
        source_count += 1
        version_count += 1
        target.execute(
            """INSERT OR IGNORE INTO knowledge_asset_binding(binding_id,asset_id,object_type,object_key,
              relation_type,status,evidence_json,created_at) VALUES (?,?,?,?,?,?,?,?)""",
            (stable_id("KAB", asset_id, "device", "device", "governs"), asset_id, "device", "device",
             "governs_device_semantics", "accepted", json.dumps({"source": "cleaning_rule_registry"}), now),
        )
        binding_count += 1

    for row in proposal_rows:
        rule_key = row["rule_key"]
        asset_id = asset_by_rule.get(rule_key)
        if asset_id is None:
            definition = {
                "canonical": row["title"] or rule_key,
                "rule_key": rule_key,
                "operation": row["operation"],
                "condition": parse_json(row["condition_json"]),
                "parameters": parse_json(row["parameters_json"]),
                "scope": parse_json(row["scope_json"]),
            }
            asset_id = upsert_asset(target, f"RULE:{rule_key}", "rule", row["title"] or rule_key, definition,
                                    row["rule_version"] or "proposal", "proposed", "HD_SAAS", now)
            asset_by_rule[rule_key] = asset_id
        status_map = {"enabled": "enabled", "rejected": "blocked", "approved": "approved", "replayed": "replayed"}
        status = status_map.get(row["status"], "draft")
        definition = {
            "canonical": row["title"] or rule_key,
            "rule_key": rule_key,
            "objective": row["objective"],
            "operation": row["operation"],
            "condition": parse_json(row["condition_json"]),
            "parameters": parse_json(row["parameters_json"]),
            "scope": parse_json(row["scope_json"]),
            "examples": parse_json(row["examples_json"]),
        }
        version_id = add_version(target, asset_id, row["rule_version"] or f"proposal-{row['proposal_id']}", definition,
                                 status, row["proposal_id"], row["evaluation_replay_id"], int(row["preview_count"] or 0),
                                 int(row["replay_count"] or 0), int(row["replay_pass_count"] or 0),
                                 int(row["replay_fail_count"] or 0), now)
        add_part(target, version_id, "condition", 1, "condition", definition["condition"], now)
        add_part(target, version_id, "rule", 1, row["operation"] or rule_key, {"operation": row["operation"], "parameters": definition["parameters"]}, now)
        add_part(target, version_id, "action", 1, "候选改写/保留原文", {"operation": row["operation"]}, now)
        add_source(target, asset_id, "rule_agent_proposal", row["proposal_id"], "rule_agent_proposal", workflow_snapshot,
                   row["status"], {"confidence": row["confidence"], "risk_level": row["risk_level"], "expected_count": row["expected_count"], "discovery_filter_status": row["discovery_filter_status"]}, now)
        source_count += 1
        version_count += 1

    for rule_key, proposals in proposal_by_rule.items():
        if len(proposals) > 1:
            asset_id = asset_by_rule.get(rule_key)
            if asset_id:
                issue_id = stable_id("KAI", asset_id, "duplicate", "proposals")
                target.execute(
                    """INSERT OR IGNORE INTO knowledge_asset_issue(issue_id,asset_id,issue_type,severity,status,details_json,created_at)
                       VALUES (?,?,?,?,?,?,?)""",
                    (issue_id, asset_id, "duplicate", "medium", "open", json.dumps({"rule_key": rule_key, "proposal_ids": [r["proposal_id"] for r in proposals]}, ensure_ascii=False), now),
                )
                issue_count += 1

    term_rows = workflow.execute("SELECT * FROM terminology_rule ORDER BY term_rule_id").fetchall()
    for row in term_rows:
        asset_key = f"TERM:{row['term_rule_id']}"
        definition = {
            "canonical": row["target_term"] or row["rule_key"],
            "rule_key": row["rule_key"],
            "rule_type": row["rule_type"],
            "source_term": row["source_term"],
            "target_term": row["target_term"],
            "context_condition": parse_json(row["context_condition_json"]),
        }
        status = "enabled" if row["status"] == "active" else "retired"
        asset_id = upsert_asset(target, asset_key, "terminology", row["rule_key"], definition,
                                row["version"] or "unversioned", status, row["site_scope"] or "all", now)
        version_id = add_version(target, asset_id, row["version"] or "unversioned", definition, status,
                                 row["term_rule_id"], None, 0, 0, 0, 0, now)
        add_part(target, version_id, "condition", 1, "适用条件", definition["context_condition"], now)
        add_part(target, version_id, "action", 1, "标准术语", {"from": row["source_term"], "to": row["target_term"]}, now)
        add_source(target, asset_id, "terminology_rule", row["term_rule_id"], "terminology_rule", workflow_snapshot,
                   row["status"], {"confirmed_by": row["confirmed_by"], "confirmed_at": row["confirmed_at"], "evidence": parse_json(row["evidence_json"])}, now)
        source_count += 1
        version_count += 1

    evaluation_rows = workflow.execute("SELECT * FROM evaluation_case ORDER BY case_id").fetchall()
    for row in evaluation_rows:
        asset_key = f"EVAL:{row['case_id']}"
        definition = {
            "canonical": row["expected_description"] or row["input_description"],
            "input_description": row["input_description"],
            "expected_decision": row["expected_decision"],
            "expected_description": row["expected_description"],
            "failure_type": row["failure_type"],
            "context": parse_json(row["context_json"]),
        }
        status = "enabled" if int(row["active"] or 0) else "retired"
        asset_id = upsert_asset(target, asset_key, "evaluation_case", row["case_id"], definition,
                                row["introduced_rule_version"] or "case-v1", status, row["source_schema"], now)
        version_id = add_version(target, asset_id, row["introduced_rule_version"] or "case-v1", definition, status,
                                 row["source_review_id"], None, 0, 0, 0, 0, now)
        add_part(target, version_id, "condition", 1, "上下文", definition["context"], now)
        add_part(target, version_id, "action", 1, row["expected_decision"], {"description": row["expected_description"]}, now)
        add_source(target, asset_id, "evaluation_case", row["case_id"], "evaluation_case", row["source_snapshot_id"],
                   "active" if row["active"] else "retired", {"source_schema": row["source_schema"], "site_id": row["site_id"], "asset_number": row["asset_number"]}, now)
        target.execute(
            """INSERT OR IGNORE INTO knowledge_asset_binding(binding_id,asset_id,object_type,object_key,
              relation_type,status,evidence_json,created_at) VALUES (?,?,?,?,?,?,?,?)""",
            (stable_id("KAB", asset_id, "device", row["source_schema"], row["site_id"], row["asset_number"]), asset_id,
             "device", f"{row['source_schema']}|{row['site_id']}|{row['asset_number']}", "evaluates_device",
             "accepted", json.dumps({"source": "evaluation_case", "source_review_id": row["source_review_id"]}, ensure_ascii=False), now),
        )
        source_count += 1
        version_count += 1
        binding_count += 1

    # Expose one generic relation table for the business-object tree without
    # copying event or device records out of their source snapshot.
    target.execute(
        """
        INSERT OR IGNORE INTO business_object_relation(
          relation_id,subject_type,subject_key,predicate,object_type,object_key,
          source_schema,source_table,source_row_id,status,confidence,evidence_json,source_snapshot_id,created_at)
        SELECT 'BOR-LINK-' || link_id,
          CASE business_type WHEN 'inspection' THEN 'inspection' WHEN 'defect' THEN 'defect' WHEN 'work_order' THEN 'work_order' ELSE 'business_event' END,
          source_schema || '|' || source_table || '|' || source_row_id,
          'recorded_for','device',unified_device_id,
          source_schema,source_table,source_row_id,status,confidence,evidence_json,source_snapshot_id,created_at
        FROM business_record_link
        """
    )
    target.execute(
        """
        INSERT OR IGNORE INTO business_object_relation(
          relation_id,subject_type,subject_key,predicate,object_type,object_key,
          source_schema,source_table,source_row_id,status,confidence,evidence_json,source_snapshot_id,created_at)
        SELECT 'BOR-DEVICE-' || relation_id,'device',subject_unified_device_id,predicate,'device',object_unified_device_id,
          source_schema,source_table,source_row_id,status,confidence,evidence_json,source_snapshot_id,created_at
        FROM unified_device_relation
        """
    )
    target.execute(
        """
        INSERT OR IGNORE INTO business_object_relation(
          relation_id,subject_type,subject_key,predicate,object_type,object_key,
          source_schema,source_table,source_row_id,status,confidence,evidence_json,source_snapshot_id,created_at)
        SELECT 'BOR-KNOWLEDGE-' || binding_id,'knowledge_asset',asset_id,relation_type,object_type,object_key,
          'LOCAL','knowledge_asset_binding',binding_id,status,1.0,evidence_json,NULL,created_at
        FROM knowledge_asset_binding
        """
    )

    # Keep this compatibility relation table useful to SQL consumers, but do
    # not let it become a second vocabulary.  Normalize every historical and
    # newly imported spelling to the stable relational relation key.  The RDF
    # local name is resolved only by semantic_registry during canonical export.
    for row in target.execute("SELECT relation_id,predicate FROM business_object_relation").fetchall():
        normalized = canonical_relation_key(row["predicate"])
        if normalized and normalized != row["predicate"]:
            target.execute(
                "UPDATE business_object_relation SET predicate=? WHERE relation_id=?",
                (normalized, row["relation_id"]),
            )

    missing_rows = target.execute(
        "SELECT asset_id FROM knowledge_asset_source WHERE source_snapshot_id IS NULL GROUP BY asset_id"
    ).fetchall()
    for row in missing_rows:
        target.execute(
            """INSERT OR IGNORE INTO knowledge_asset_issue(issue_id,asset_id,issue_type,severity,status,details_json,created_at)
               VALUES (?,?,?,?,?,?,?)""",
            (stable_id("KAI", row["asset_id"], "missing_snapshot"), row["asset_id"], "missing_evidence", "low", "open",
             json.dumps({"reason": "source_snapshot_id_missing"}), now),
        )
        issue_count += 1

    run_id = stable_id("KLR", workflow_db, identity_db, now)
    target.execute(
        """INSERT INTO knowledge_layer_run(run_id,workflow_db_path,identity_db_path,asset_count,version_count,
          source_count,binding_count,issue_count,source_write,formal_publication,created_at)
          VALUES (?,?,?,?,?,?,?, ?,0,0,?)""",
        (run_id, str(workflow_db.resolve()), str(identity_db.resolve()),
         int(target.execute("SELECT count(*) FROM knowledge_asset").fetchone()[0]),
         int(target.execute("SELECT count(*) FROM knowledge_asset_version").fetchone()[0]),
         int(target.execute("SELECT count(*) FROM knowledge_asset_source").fetchone()[0]),
         int(target.execute("SELECT count(*) FROM knowledge_asset_binding").fetchone()[0]),
         int(target.execute("SELECT count(*) FROM knowledge_asset_issue").fetchone()[0]), now),
    )
    target.commit()
    target.close()
    workflow.close()
    identity.close()
    verify = sqlite3.connect(str(target_db))
    asset_count = int(verify.execute("SELECT count(*) FROM knowledge_asset").fetchone()[0])
    version_total = int(verify.execute("SELECT count(*) FROM knowledge_asset_version").fetchone()[0])
    verify.close()
    return {
        "run_id": run_id,
        "target_db": str(target_db.resolve()),
        "asset_count": asset_count,
        "version_count": version_total,
        "source_write": False,
        "formal_publication": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Build relational business-object and knowledge identity layer")
    parser.add_argument("--workflow-db", type=pathlib.Path, default=DEFAULT_WORKFLOW)
    parser.add_argument("--identity-db", type=pathlib.Path, default=None)
    parser.add_argument("--target-db", type=pathlib.Path, default=DEFAULT_TARGET)
    args = parser.parse_args()
    identity_db = (args.identity_db or latest_identity_db()).resolve()
    print(json.dumps(build(args.workflow_db.resolve(), identity_db, args.target_db.resolve()), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
