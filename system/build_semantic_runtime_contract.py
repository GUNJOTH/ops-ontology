"""Materialize the shared runtime contract for objects, sources, identities and events.

This is the relational implementation of the world-model ideas: source data
remains authoritative and read-only, local mappings and derived layers have
explicit precedence, and every accepted event can be traced through a stable
correlation and previous-event chain.  It is additive and idempotent.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import sqlite3
from datetime import datetime, timezone


ROOT = pathlib.Path(__file__).resolve().parent
DEFAULT_TARGET = ROOT / "data" / "unified_semantics.sqlite3"
CONTRACT_VERSION = "semantic-runtime-contract-v1"


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sid(prefix: str, *parts: object) -> str:
    raw = "|".join("" if part is None else str(part) for part in parts)
    return f"{prefix}-{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:24]}"


def parse_json(value: object) -> dict[str, object]:
    try:
        parsed = json.loads(value or "{}") if isinstance(value, str) else value
    except (TypeError, json.JSONDecodeError):
        parsed = {}
    return parsed if isinstance(parsed, dict) else {}


def safe_confidence(value: object) -> tuple[float, bool]:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 0.0, False
    if parsed != parsed or parsed < 0 or parsed > 1:
        return 0.0, False
    return parsed, True


def table_exists(db: sqlite3.Connection, table_name: str) -> bool:
    return db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone() is not None


def ensure_schema(db: sqlite3.Connection) -> None:
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS semantic_layer_authority (
          layer_key TEXT PRIMARY KEY,
          layer_kind TEXT NOT NULL CHECK(layer_kind IN ('source','normalized','derived','decision','action')),
          display_name TEXT NOT NULL,
          precedence INTEGER NOT NULL,
          source_of_record INTEGER NOT NULL CHECK(source_of_record IN (0,1)),
          can_write_source INTEGER NOT NULL CHECK(can_write_source=0),
          requires_approval INTEGER NOT NULL CHECK(requires_approval IN (0,1)),
          applies_to_json TEXT NOT NULL,
          status TEXT NOT NULL CHECK(status IN ('active','blocked')),
          version TEXT NOT NULL,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS semantic_object_instance (
          object_id TEXT PRIMARY KEY,
          object_type TEXT NOT NULL,
          canonical_key TEXT NOT NULL,
          display_name TEXT NOT NULL,
          source_namespace TEXT,
          status TEXT NOT NULL CHECK(status IN ('accepted','needs_review','blocked')),
          evidence_json TEXT NOT NULL,
          source_snapshot_id TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          UNIQUE(object_type,canonical_key)
        );
        CREATE INDEX IF NOT EXISTS ix_semantic_object_instance_type
          ON semantic_object_instance(object_type,status);
        CREATE TABLE IF NOT EXISTS semantic_identity_assertion (
          assertion_id TEXT PRIMARY KEY,
          source_system TEXT NOT NULL,
          source_schema TEXT NOT NULL,
          source_table_group TEXT NOT NULL,
          source_table TEXT NOT NULL,
          source_row_id TEXT NOT NULL,
          source_key_type TEXT NOT NULL,
          source_key TEXT NOT NULL,
          canonical_object_type TEXT NOT NULL,
          canonical_object_id TEXT NOT NULL,
          assertion_type TEXT NOT NULL CHECK(assertion_type IN ('source_record_link','manual','derived','temporal')),
          status TEXT NOT NULL CHECK(status IN ('accepted','needs_review','rejected')),
          confidence REAL NOT NULL CHECK(confidence >= 0 AND confidence <= 1),
          valid_from TEXT,
          valid_to TEXT,
          evidence_json TEXT NOT NULL,
          source_snapshot_id TEXT NOT NULL,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          UNIQUE(source_schema,source_table,source_row_id,canonical_object_id)
        );
        CREATE INDEX IF NOT EXISTS ix_semantic_identity_assertion_source
          ON semantic_identity_assertion(source_schema,source_key_type,source_key,status);
        CREATE INDEX IF NOT EXISTS ix_semantic_identity_assertion_canonical
          ON semantic_identity_assertion(canonical_object_type,canonical_object_id,status);
        CREATE TABLE IF NOT EXISTS semantic_constraint_violation (
          violation_id TEXT PRIMARY KEY,
          constraint_key TEXT NOT NULL,
          entity_type TEXT NOT NULL,
          entity_id TEXT NOT NULL,
          severity TEXT NOT NULL CHECK(severity IN ('low','medium','high','critical')),
          status TEXT NOT NULL CHECK(status IN ('open','isolated','resolved')),
          expected_json TEXT NOT NULL,
          actual_json TEXT NOT NULL,
          evidence_json TEXT NOT NULL,
          created_at TEXT NOT NULL,
          resolved_at TEXT,
          UNIQUE(constraint_key,entity_type,entity_id)
        );
        CREATE INDEX IF NOT EXISTS ix_semantic_constraint_violation_status
          ON semantic_constraint_violation(status,severity,constraint_key);
        CREATE TABLE IF NOT EXISTS semantic_runtime_contract_run (
          run_id TEXT PRIMARY KEY,
          contract_version TEXT NOT NULL,
          authority_count INTEGER NOT NULL,
          object_instance_count INTEGER NOT NULL,
          identity_assertion_count INTEGER NOT NULL,
          event_count INTEGER NOT NULL,
          traced_event_count INTEGER NOT NULL,
          violation_count INTEGER NOT NULL,
          isolated_count INTEGER NOT NULL,
          status TEXT NOT NULL CHECK(status IN ('completed','completed_with_isolation','blocked')),
          source_write INTEGER NOT NULL CHECK(source_write=0),
          formal_publication INTEGER NOT NULL CHECK(formal_publication=0),
          created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_semantic_runtime_contract_run_created
          ON semantic_runtime_contract_run(created_at);
        """
    )
    columns = {row[1] for row in db.execute("PRAGMA table_info(semantic_event)").fetchall()}
    for name, definition in {
        "correlation_id": "TEXT",
        "previous_event_id": "TEXT",
        "actor_type": "TEXT",
        "actor_key": "TEXT",
        "event_version": "INTEGER NOT NULL DEFAULT 1",
        "provenance_hash": "TEXT",
    }.items():
        if name not in columns:
            db.execute(f"ALTER TABLE semantic_event ADD COLUMN {name} {definition}")


def seed_authority(db: sqlite3.Connection, created: str) -> None:
    rows = [
        ("SOURCE_RAW", "source", "源系统原始事实", 100, 1, 0, 0, ["source_fact", "semantic_event"], "源系统是原始值权威；本地层不回写"),
        ("CANONICAL_RDF_DATASET", "derived", "Canonical RDF 语义权威", 90, 1, 0, 0, ["canonical_projection", "canonical_statement", "canonical_inference_run"], "标准语义表达、命名图和推理结果的权威；由只读源快照投影生成"),
        ("IDENTITY_ASSERTION", "normalized", "身份与对象映射", 80, 0, 0, 1, ["semantic_identity_assertion", "business_record_link"], "必须保留来源、证据和有效期"),
        ("DERIVED_FACT", "derived", "确定性派生事实", 60, 0, 0, 0, ["semantic_fact", "semantic_current_state"], "只能由来源事实和已启用规则推导"),
        ("RULE_DECISION", "decision", "规则判断", 40, 0, 0, 1, ["semantic_rule_decision"], "判断可解释、可回放，不能冒充源事实"),
        ("ACTION_PLAN", "action", "行动计划", 20, 0, 0, 1, ["semantic_action_plan", "semantic_execution_ledger"], "必须审批；外部执行器默认关闭"),
    ]
    for key, kind, name, precedence, source_record, can_write, approval, applies, note in rows:
        db.execute(
            """INSERT INTO semantic_layer_authority(
              layer_key,layer_kind,display_name,precedence,source_of_record,can_write_source,
              requires_approval,applies_to_json,status,version,created_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?, 'active',?,?,?)
            ON CONFLICT(layer_key) DO UPDATE SET layer_kind=excluded.layer_kind,
              display_name=excluded.display_name,precedence=excluded.precedence,
              source_of_record=excluded.source_of_record,can_write_source=excluded.can_write_source,
              requires_approval=excluded.requires_approval,applies_to_json=excluded.applies_to_json,
              status=excluded.status,version=excluded.version,updated_at=excluded.updated_at""",
            (key, kind, name, precedence, source_record, can_write, approval,
             json.dumps({"tables": applies, "policy": note}, ensure_ascii=False), CONTRACT_VERSION, created, created),
        )


def upsert_object(db: sqlite3.Connection, object_type: str, canonical_key: str, display_name: str,
                  source_namespace: str | None, status: str, evidence: dict[str, object],
                  snapshot_id: str | None, created: str) -> None:
    object_id = sid("OBJ", object_type, canonical_key)
    db.execute(
        """INSERT INTO semantic_object_instance(
          object_id,object_type,canonical_key,display_name,source_namespace,status,
          evidence_json,source_snapshot_id,created_at,updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(object_type,canonical_key) DO UPDATE SET display_name=excluded.display_name,
          source_namespace=excluded.source_namespace,status=excluded.status,
          evidence_json=excluded.evidence_json,source_snapshot_id=excluded.source_snapshot_id,
          updated_at=excluded.updated_at""",
        (object_id, object_type, canonical_key, display_name, source_namespace, status,
         json.dumps(evidence, ensure_ascii=False, sort_keys=True), snapshot_id, created, created),
    )


def build(target_path: pathlib.Path) -> dict[str, object]:
    db = sqlite3.connect(str(target_path), timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout=30000")
    ensure_schema(db)
    created = now()
    seed_authority(db, created)

    identity_rows = db.execute("SELECT * FROM business_record_link ORDER BY link_id").fetchall()
    for row in identity_rows:
        device_id = row["unified_device_id"] or f"{row['source_schema']}:{row['source_key']}"
        upsert_object(db, "device", str(device_id), str(device_id), row["source_schema"], row["status"],
                      {"source_table": row["source_table"], "source_row_id": row["source_row_id"], "identity_link_id": row["link_id"]},
                      row["source_snapshot_id"], created)
        record_key = f"{row['source_schema']}|{row['source_table']}|{row['source_row_id']}"
        upsert_object(db, row["business_type"], record_key, record_key, row["source_schema"], row["status"],
                      {"source_key_type": row["source_key_type"], "source_key": row["source_key"], "link_id": row["link_id"]},
                      row["source_snapshot_id"], created)

        evidence = parse_json(row["evidence_json"])
        valid_from = evidence.get("valid_from") or evidence.get("effective_from")
        valid_to = evidence.get("valid_to") or evidence.get("effective_to")
        assertion_status = row["status"] if row["status"] in {"accepted", "needs_review", "rejected"} else "needs_review"
        db.execute(
            """INSERT INTO semantic_identity_assertion(
              assertion_id,source_system,source_schema,source_table_group,source_table,source_row_id,
              source_key_type,source_key,canonical_object_type,canonical_object_id,assertion_type,
              status,confidence,valid_from,valid_to,evidence_json,source_snapshot_id,created_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(source_schema,source_table,source_row_id,canonical_object_id) DO UPDATE SET
              status=excluded.status,confidence=excluded.confidence,valid_from=excluded.valid_from,
              valid_to=excluded.valid_to,evidence_json=excluded.evidence_json,
              source_snapshot_id=excluded.source_snapshot_id,updated_at=excluded.updated_at""",
            (sid("ASSERT", row["source_schema"], row["source_table"], row["source_row_id"], device_id),
             row["source_schema"], row["source_schema"], row["source_table_group"], row["source_table"], row["source_row_id"],
             row["source_key_type"], row["source_key"], "device", device_id, "temporal" if valid_from or valid_to else "source_record_link",
             assertion_status if safe_confidence(row["confidence"])[1] else "needs_review",
             safe_confidence(row["confidence"])[0], valid_from, valid_to, row["evidence_json"] or "{}", row["source_snapshot_id"], created, created),
        )

    events = db.execute("SELECT * FROM semantic_event ORDER BY subject_type,subject_key,occurred_at,recorded_at,event_id").fetchall()
    grouped: dict[tuple[str, str, str, str], list[sqlite3.Row]] = {}
    traced = 0
    for event in events:
        payload = parse_json(event["payload_json"])
        source_event = payload.get("source_event") if isinstance(payload.get("source_event"), dict) else {}
        correlation_value = source_event.get("correlation_id") or source_event.get("correlationId") or payload.get("correlation_id") or payload.get("correlationId")
        correlation = str(correlation_value).strip() if correlation_value is not None and str(correlation_value).strip() else None
        actor_type = source_event.get("actor_type") or source_event.get("actorType")
        actor_key = source_event.get("actor_key") or source_event.get("actor_id") or source_event.get("actorId")
        provenance = {
            "event_id": event["event_id"],
            "source_schema": event["source_schema"],
            "source_table": event["source_table"],
            "source_row_id": event["source_row_id"],
            "source_snapshot_id": event["source_snapshot_id"],
        }
        provenance_hash = hashlib.sha256(json.dumps(provenance, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
        db.execute(
            """UPDATE semantic_event SET correlation_id=?,actor_type=?,actor_key=?,event_version=1,provenance_hash=? WHERE event_id=?""",
            (correlation, actor_type, actor_key, provenance_hash, event["event_id"]),
        )
        grouped.setdefault((event["subject_type"], event["subject_key"], event["source_schema"], event["source_snapshot_id"]), []).append(event)
        if event["source_schema"] and event["source_table"] and event["source_row_id"] and event["source_snapshot_id"] and event["subject_key"]:
            traced += 1

    for items in grouped.values():
        ordered = sorted(items, key=lambda row: (str(row["occurred_at"] or "9999-12-31"), str(row["recorded_at"] or "9999-12-31"), row["event_id"]))
        previous: str | None = None
        for event in ordered:
            db.execute("UPDATE semantic_event SET previous_event_id=? WHERE event_id=?", (previous, event["event_id"]))
            previous = event["event_id"]

    db.execute("DELETE FROM semantic_constraint_violation")
    violations: list[tuple[str, str, str, str, str, str, str, str, str]] = []
    for event in events:
        if not all(str(event[key] or "").strip() for key in ("source_schema", "source_table", "source_row_id", "source_snapshot_id", "subject_key")):
            violations.append(("event_provenance_required", "semantic_event", event["event_id"], "high", "isolated", json.dumps({"required": "source fields"}), json.dumps(dict(event), ensure_ascii=False), json.dumps({"event_id": event["event_id"]}, ensure_ascii=False), created))
    if table_exists(db, "source_system"):
        source_system_rows = db.execute("SELECT system_key,read_only FROM source_system").fetchall()
        for row in source_system_rows:
            if int(row["read_only"] or 0) != 1:
                violations.append(("source_read_only", "source_system", row["system_key"], "critical", "isolated", json.dumps({"read_only": 1}), json.dumps({"read_only": row["read_only"]}), json.dumps({"source_system": row["system_key"]}), created))
    else:
        violations.append(("source_registry_missing", "source_system", "registry", "high", "isolated", json.dumps({"required": "source_system"}), json.dumps({"table": None}), json.dumps({"action": "isolate_until_source_registry_exists"}), created))
    registered_predicates = {
        str(row["predicate"])
        for row in db.execute("SELECT predicate FROM ontology_relation_type WHERE status='active' AND review_status='approved'").fetchall()
    }
    for row in db.execute("SELECT relation_id,predicate FROM business_object_relation WHERE status='accepted'").fetchall():
        if str(row["predicate"]) not in registered_predicates:
            violations.append(("relation_predicate_registered", "business_object_relation", row["relation_id"], "high", "isolated", json.dumps({"predicate": row["predicate"]}), json.dumps({"predicate": row["predicate"]}), json.dumps({"relation_id": row["relation_id"]}), created))
    for row in db.execute("SELECT fact_id,source_schema,source_table,source_row_id FROM semantic_fact WHERE status<>'retracted'").fetchall():
        if not all(str(row[key] or "").strip() for key in ("source_schema", "source_table", "source_row_id")):
            violations.append(("fact_provenance_required", "semantic_fact", row["fact_id"], "high", "isolated", json.dumps({"required": "source fields"}), json.dumps(dict(row), ensure_ascii=False), json.dumps({"fact_id": row["fact_id"]}, ensure_ascii=False), created))
    for item in violations:
        db.execute(
            """INSERT INTO semantic_constraint_violation(
              violation_id,constraint_key,entity_type,entity_id,severity,status,
              expected_json,actual_json,evidence_json,created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (sid("VIOL", *item[:3]), *item),
        )

    object_count = int(db.execute("SELECT count(*) FROM semantic_object_instance").fetchone()[0])
    assertion_count = int(db.execute("SELECT count(*) FROM semantic_identity_assertion").fetchone()[0])
    authority_count = int(db.execute("SELECT count(*) FROM semantic_layer_authority WHERE status='active'").fetchone()[0])
    violation_count = int(db.execute("SELECT count(*) FROM semantic_constraint_violation").fetchone()[0])
    isolated_count = int(db.execute("SELECT count(*) FROM semantic_constraint_violation WHERE status='isolated'").fetchone()[0])
    run_id = sid("SRCR", CONTRACT_VERSION, created)
    status = "completed_with_isolation" if isolated_count else "completed"
    db.execute(
        """INSERT INTO semantic_runtime_contract_run(
          run_id,contract_version,authority_count,object_instance_count,identity_assertion_count,
          event_count,traced_event_count,violation_count,isolated_count,status,source_write,formal_publication,created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,0,0,?)""",
        (run_id, CONTRACT_VERSION, authority_count, object_count, assertion_count, len(events), traced, violation_count, isolated_count, status, created),
    )
    db.commit()
    db.close()
    return {
        "run_id": run_id,
        "contract_version": CONTRACT_VERSION,
        "authority_count": authority_count,
        "object_instance_count": object_count,
        "identity_assertion_count": assertion_count,
        "event_count": len(events),
        "traced_event_count": traced,
        "violation_count": violation_count,
        "isolated_count": isolated_count,
        "status": status,
        "source_write": False,
        "formal_publication": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the local source-authority, object and identity runtime contract")
    parser.add_argument("--target-db", type=pathlib.Path, default=DEFAULT_TARGET)
    args = parser.parse_args()
    print(json.dumps(build(args.target_db.resolve()), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
