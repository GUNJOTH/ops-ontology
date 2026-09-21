"""Create an auditable, read-only backlog report for handoff and triage."""
from __future__ import annotations

import json
import pathlib
import sqlite3
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parent
WORKFLOW_DB = ROOT / "data" / "semantic_workflow.sqlite3"
METADATA_DB = ROOT.parent / "pilots" / "metadata" / "results" / "metadata-semantic-v1-20260815T-v2" / "metadata_semantics.sqlite3"
OUTPUT = ROOT / "data" / "governance_backlog_report.json"


def grouped(db: sqlite3.Connection, sql: str) -> list[dict[str, object]]:
    return [dict(row) for row in db.execute(sql).fetchall()]


def main() -> None:
    workflow = sqlite3.connect(str(WORKFLOW_DB))
    workflow.row_factory = sqlite3.Row
    try:
        queue_status = grouped(workflow, "SELECT status,count(*) AS count FROM formal_approval_queue GROUP BY status ORDER BY status")
        queue_decisions = grouped(workflow, "SELECT proposed_decision,status,count(*) AS count FROM formal_approval_queue GROUP BY proposed_decision,status ORDER BY proposed_decision,status")
        ready = int(workflow.execute(
            """
            SELECT count(*) FROM formal_approval_queue q
            JOIN semantic_candidate c ON c.candidate_id=q.candidate_id
            JOIN replay_run r ON r.replay_id=q.replay_id
            WHERE q.status='pending' AND q.proposed_decision='approved'
              AND r.status='passed' AND r.fail_count=0
              AND c.validator_status='candidate' AND c.review_state='pending'
              AND c.publication_state='unpublished'
              AND q.source_write=0 AND q.formal_publication=0
            """
        ).fetchone()[0])
        modified_pending = int(workflow.execute(
            "SELECT count(*) FROM formal_approval_queue WHERE status='pending' AND proposed_decision='modified'"
        ).fetchone()[0])
        cleaning = grouped(workflow, "SELECT status,count(*) AS count FROM cleaning_run GROUP BY status ORDER BY status")
        agent_runs = grouped(workflow, "SELECT status,count(*) AS count FROM rule_agent_run GROUP BY status ORDER BY status")
        agent_errors = grouped(workflow, "SELECT coalesce(error_code,'unclassified') AS error_code,retryable,count(*) AS count FROM rule_agent_run WHERE status='failed' GROUP BY error_code,retryable ORDER BY error_code")
    finally:
        workflow.close()

    metadata: dict[str, object]
    if METADATA_DB.exists():
        meta = sqlite3.connect(str(METADATA_DB))
        meta.row_factory = sqlite3.Row
        try:
            dictionary_count = int(meta.execute("SELECT count(*) FROM metadata_semantic_dictionary").fetchone()[0])
            finding_count = int(meta.execute("SELECT count(*) FROM metadata_validation_findings").fetchone()[0])
            metadata = {
                "dictionary_count": dictionary_count,
                "semantic_statuses": grouped(meta, "SELECT coalesce(semantic_status,'unclassified') AS status,count(*) AS count FROM metadata_semantic_dictionary GROUP BY status ORDER BY status"),
                "ai_categories": grouped(meta, "SELECT coalesce(ai_category,'unclassified') AS category,count(*) AS count FROM metadata_semantic_dictionary GROUP BY category ORDER BY category"),
                "finding_count": finding_count,
                "finding_types": grouped(meta, "SELECT finding_type,count(*) AS count FROM metadata_validation_findings GROUP BY finding_type ORDER BY finding_type"),
                "finding_types_by_source": grouped(meta, "SELECT finding_type,source_schema,count(*) AS count FROM metadata_validation_findings GROUP BY finding_type,source_schema ORDER BY finding_type,source_schema"),
                "triage_policy": {
                    "attribute_parent_missing": "先按 OBJECTNAME/ENTITYNAME/SAMEASOBJECT/GRPTABLE 复核；不能证明父级的继续隔离",
                    "object_table_unmatched": "区分逻辑对象、视图、系统扩展对象和真实缺失的 GRPTABLE 绑定",
                    "index_definition_missing": "按索引名、表名、租户和列序号复核 GRPSYSKEYS 与 GRPSYSINDEXES；不直接删除索引列证据",
                },
                "source_write": False,
                "formal_publication": False,
            }
        finally:
            meta.close()
    else:
        metadata = {"status": "missing_result_db", "source_write": False, "formal_publication": False}

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": "triage_required",
        "approval": {
            "queue_status": queue_status,
            "queue_decisions": queue_decisions,
            "ready_for_human_batch_approval": ready,
            "pending_modified_review": modified_pending,
            "policy": "仅回放通过、身份有效且未发布记录可进入人工批量审批；本报告不自动审批。",
        },
        "cleaning_tasks": {
            "statuses": cleaning,
            "policy": "保留任务、回放和审批审计；未得到明确归档范围前不删除历史任务。",
        },
        "rule_agent": {"runs": agent_runs, "errors": agent_errors},
        "metadata": metadata,
        "source_write": False,
        "formal_publication": False,
        "next_gate": "先按 replay_id/site 分层抽查并批量人工审批；元数据按 finding_type 逐类确认或继续隔离。",
    }
    OUTPUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    from pipeline.legacy import run_legacy_main

    raise SystemExit(
        run_legacy_main(
            pipeline_id="diagnose-governance-backlog",
            pipeline_version="v1",
            root=ROOT,
            legacy_main=main,
        )
    )
