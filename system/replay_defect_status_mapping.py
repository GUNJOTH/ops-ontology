"""Replay approved HD/XNY defect-status mappings into local semantic facts."""
from __future__ import annotations

import argparse
import json
import pathlib
import sqlite3

from common import DEFAULT_TARGET, sid
from common import utc_now as now
from pipeline.contracts import connect_local

ROOT = pathlib.Path(__file__).resolve().parent


def ensure_schema(db: sqlite3.Connection) -> None:
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS semantic_status_replay_run (
          run_id TEXT PRIMARY KEY,
          approved_mapping_count INTEGER NOT NULL,
          input_fact_count INTEGER NOT NULL,
          matched_fact_count INTEGER NOT NULL,
          decision_count INTEGER NOT NULL,
          derived_fact_count INTEGER NOT NULL,
          action_count INTEGER NOT NULL,
          status TEXT NOT NULL CHECK (status IN ('completed','blocked','needs_review')),
          note TEXT NOT NULL,
          source_write INTEGER NOT NULL CHECK (source_write=0),
          formal_publication INTEGER NOT NULL CHECK (formal_publication=0),
          created_at TEXT NOT NULL
        )
        """
    )


def replay(target_path: pathlib.Path) -> dict[str, object]:
    db = connect_local(str(target_path), timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout=30000")
    ensure_schema(db)
    created = now()
    approved = db.execute(
        """
        SELECT * FROM semantic_status_dictionary
        WHERE mapping_status='approved'
          AND canonical_state IS NOT NULL
          AND length(trim(canonical_state))>0
          AND mapping_version>0
        ORDER BY source_schema,source_table,status_id
        """
    ).fetchall()
    canonical_states = {
        row["canonical_state"]: dict(row)
        for row in db.execute(
            "SELECT * FROM semantic_canonical_state WHERE state_domain='DEFECT' AND status='active'"
        ).fetchall()
    }
    approved = [row for row in approved if row["canonical_state"] in canonical_states]
    input_facts = db.execute(
        "SELECT * FROM semantic_fact WHERE fact_type='defect_status' AND status IN ('observed','accepted') ORDER BY fact_id"
    ).fetchall()
    if not approved:
        run_id = sid("SSRL", created)
        note = "没有已确认的状态映射，回放阻塞；请先在缺陷状态字典中确认业务含义"
        db.execute(
            """
            INSERT INTO semantic_status_replay_run(
              run_id,approved_mapping_count,input_fact_count,matched_fact_count,decision_count,
              derived_fact_count,action_count,status,note,source_write,formal_publication,created_at
            ) VALUES (?,?,?,?,?,?,?,'blocked',?,0,0,?)
            """,
            (run_id, 0, len(input_facts), 0, 0, 0, 0, note, created),
        )
        db.commit()
        db.close()
        return {"run_id": run_id, "approved_mapping_count": 0, "input_fact_count": len(input_facts), "matched_fact_count": 0, "decision_count": 0, "derived_fact_count": 0, "action_count": 0, "status": "blocked", "note": note, "source_write": False, "formal_publication": False}

    mapping_index = {
        (row["source_schema"], row["source_table"], row["raw_status"], row["source_snapshot_id"]): row
        for row in approved
    }
    matched = 0
    for fact in input_facts:
        try:
            payload = json.loads(fact["value_json"])
        except (TypeError, json.JSONDecodeError):
            payload = {}
        raw_status = payload.get("raw_status") if isinstance(payload, dict) else None
        mapping = mapping_index.get(
            (fact["source_schema"], fact["source_table"], raw_status or "", fact["source_snapshot_id"])
        )
        if mapping is None:
            continue
        matched += 1
        canonical = canonical_states[mapping["canonical_state"]]
        rule_asset_id = f"SBR:dictionary-status:{mapping['status_id']}"
        rule_version_id = f"SBRV:dictionary-status:{mapping['status_id']}:v{mapping['mapping_version']}"
        explanation = (
            f"状态映射由本地字典 {mapping['status_id']} v{mapping['mapping_version']} 经人工确认，"
            f"保留原始状态值并规范化为 {mapping['canonical_state']}。"
        )
        previous_rule = db.execute(
            "SELECT rule_version_id FROM semantic_logic_rule WHERE rule_asset_id=?",
            (rule_asset_id,),
        ).fetchone()
        if previous_rule is not None and previous_rule["rule_version_id"] != rule_version_id:
            db.execute(
                "UPDATE semantic_rule_decision SET status='rejected' WHERE rule_asset_id=? AND rule_version_id<>?",
                (rule_asset_id, rule_version_id),
            )
            db.execute(
                "UPDATE semantic_fact_derivation SET status='rejected' WHERE rule_asset_id=? AND rule_version_id<>?",
                (rule_asset_id, rule_version_id),
            )
        db.execute(
            """
            INSERT INTO semantic_logic_rule(
              rule_asset_id,rule_version_id,input_fact_type,input_predicate,
              output_fact_type,output_predicate,decision,explanation,status,
              requires_approval,source_write,created_at
            ) VALUES (?,?,?,?,?,?,?,?, 'enabled',0,0,?)
            ON CONFLICT(rule_asset_id) DO UPDATE SET
              rule_version_id=excluded.rule_version_id,
              input_fact_type=excluded.input_fact_type,
              input_predicate=excluded.input_predicate,
              output_fact_type=excluded.output_fact_type,
              output_predicate=excluded.output_predicate,
              decision=excluded.decision,
              status='enabled',
              explanation=excluded.explanation
            """,
            (rule_asset_id, rule_version_id, "defect_status", "has_status", "canonical_defect_state", "has_canonical_defect_state", "defect_state_normalized", explanation, created),
        )
        decision_id = sid("SRD", rule_version_id, fact["fact_id"])
        derived_fact_id = sid("FACT", "canonical_defect_state", fact["subject_key"], fact["fact_id"], mapping["status_id"])
        derivation_id = sid("SFD", rule_version_id, fact["fact_id"])
        context = {
            "subject_type": fact["subject_type"],
            "subject_key": fact["subject_key"],
            "raw_status": raw_status,
            "canonical_state": mapping["canonical_state"],
            "business_meaning": mapping["business_meaning"],
            "dictionary_status_id": mapping["status_id"],
            "mapping_version": mapping["mapping_version"],
        }
        db.execute(
            """
            INSERT OR IGNORE INTO semantic_rule_decision(
              decision_id,subject_type,subject_key,rule_asset_id,rule_version_id,
              input_fact_ids_json,input_context_json,decision,confidence,explanation,
              status,requires_action,created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?, 'accepted',0,?)
            ON CONFLICT(decision_id) DO UPDATE SET
              input_context_json=excluded.input_context_json,
              decision=excluded.decision,
              confidence=excluded.confidence,
              explanation=excluded.explanation,
              status='accepted'
            """,
            (decision_id, fact["subject_type"], fact["subject_key"], rule_asset_id, rule_version_id, json.dumps([fact["fact_id"]]), json.dumps(context, ensure_ascii=False), "defect_state_normalized", fact["confidence"], explanation, created),
        )
        derived_value = {
            "raw_status": raw_status,
            "canonical_state": mapping["canonical_state"],
            "canonical_display_name": canonical["display_name"],
            "business_meaning": mapping["business_meaning"],
            "dictionary_status_id": mapping["status_id"],
            "mapping_version": mapping["mapping_version"],
            "derived_from_fact_id": fact["fact_id"],
            "decision_id": decision_id,
            "fact_class": "NormalizedFact",
            "evidence_only": False,
        }
        db.execute(
            """
            INSERT INTO semantic_fact(
              fact_id,fact_type,subject_type,subject_key,predicate,value_json,unit,
              source_schema,source_table,source_row_id,source_snapshot_id,status,
              confidence,observed_at,created_at
            ) VALUES (?,?,?,?,?,?,NULL,'LOCAL_SEMANTIC','semantic_status_dictionary',?,?, 'derived',?,?,?)
            ON CONFLICT(fact_type,subject_key,predicate,source_schema,source_table,source_row_id) DO UPDATE SET
              value_json=excluded.value_json,
              source_snapshot_id=excluded.source_snapshot_id,
              status='derived',
              confidence=excluded.confidence,
              created_at=excluded.created_at
            """,
            (derived_fact_id, "canonical_defect_state", fact["subject_type"], fact["subject_key"], "has_canonical_defect_state", json.dumps(derived_value, ensure_ascii=False), mapping["status_id"], fact["source_snapshot_id"], fact["confidence"], None, created),
        )
        db.execute(
            """
            INSERT INTO semantic_fact_derivation(
              derivation_id,output_fact_id,rule_asset_id,rule_version_id,
              input_fact_ids_json,input_context_json,decision_id,explanation,
              constraint_results_json,status,created_at
            ) VALUES (?,?,?,?,?,?,?,?,?, 'accepted',?)
            ON CONFLICT(derivation_id) DO UPDATE SET
              input_context_json=excluded.input_context_json,
              decision_id=excluded.decision_id,
              explanation=excluded.explanation,
              constraint_results_json=excluded.constraint_results_json,
              status='accepted'
            """,
            (derivation_id, derived_fact_id, rule_asset_id, rule_version_id, json.dumps([fact["fact_id"]]), json.dumps(context, ensure_ascii=False), decision_id, explanation, json.dumps({"mapping_status": "approved", "canonical_state": mapping["canonical_state"], "mapping_version": mapping["mapping_version"], "source_write": "pass", "formal_publication": "pass"}), created),
        )

    run_id = sid("SSRL", created)
    status = "completed" if matched else "needs_review"
    note = "已按人工确认字典生成标准缺陷状态事实；未生成行动" if matched else "存在已确认映射但没有匹配输入事实"
    db.execute(
        """
        INSERT INTO semantic_status_replay_run(
          run_id,approved_mapping_count,input_fact_count,matched_fact_count,decision_count,
          derived_fact_count,action_count,status,note,source_write,formal_publication,created_at
        ) VALUES (?,?,?,?,?,?,?, ?,?,0,0,?)
        """,
        (run_id, len(approved), len(input_facts), matched, matched, matched, 0, status, note, created),
    )
    db.commit()
    db.close()
    return {"run_id": run_id, "approved_mapping_count": len(approved), "input_fact_count": len(input_facts), "matched_fact_count": matched, "decision_count": matched, "derived_fact_count": matched, "action_count": 0, "status": status, "note": note, "source_write": False, "formal_publication": False}


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay approved defect status mappings locally")
    parser.add_argument("--target-db", type=pathlib.Path, default=DEFAULT_TARGET)
    args = parser.parse_args()
    print(json.dumps(replay(args.target_db.resolve()), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()