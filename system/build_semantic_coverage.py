"""Build an auditable coverage and closure report for the local semantic layer.

The report answers a different question from a row-count check: which
registered business concepts have real local evidence, which are absent from
the current snapshot, and which backlogs are intentionally waiting at a
review gate.  It reads the local overlay plus the two local backlog stores;
it never connects to or writes a source system.
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
DEFAULT_WORKFLOW = ROOT / "data" / "semantic_workflow.sqlite3"
DEFAULT_METADATA = ROOT.parent / "pilots" / "metadata" / "results" / "metadata-semantic-v1-20260815T-v2" / "metadata_semantics.sqlite3"
COVERAGE_VERSION = "semantic-coverage-v1"


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sid(prefix: str, *parts: object) -> str:
    raw = "|".join("" if part is None else str(part) for part in parts)
    return f"{prefix}-{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:24]}"


def ensure_schema(db: sqlite3.Connection) -> None:
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS semantic_coverage_run (
          run_id TEXT PRIMARY KEY,
          coverage_version TEXT NOT NULL,
          source_system_count INTEGER NOT NULL,
          source_fact_count INTEGER NOT NULL,
          event_count INTEGER NOT NULL,
          subject_count INTEGER NOT NULL,
          event_type_registered_count INTEGER NOT NULL,
          event_type_observed_count INTEGER NOT NULL,
          event_type_missing_count INTEGER NOT NULL,
          fact_count INTEGER NOT NULL,
          derived_fact_count INTEGER NOT NULL,
          risk_fact_count INTEGER NOT NULL,
          decision_count INTEGER NOT NULL,
          action_plan_count INTEGER NOT NULL,
          approval_pending_count INTEGER NOT NULL,
          metadata_finding_count INTEGER NOT NULL,
          gap_count INTEGER NOT NULL,
          status TEXT NOT NULL CHECK(status IN ('completed','partial','blocked')),
          report_json TEXT NOT NULL,
          source_write INTEGER NOT NULL CHECK(source_write=0),
          formal_publication INTEGER NOT NULL CHECK(formal_publication=0),
          created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_semantic_coverage_run_created
          ON semantic_coverage_run(created_at);
        CREATE TABLE IF NOT EXISTS semantic_coverage_gap (
          gap_id TEXT PRIMARY KEY,
          run_id TEXT NOT NULL REFERENCES semantic_coverage_run(run_id),
          gap_type TEXT NOT NULL,
          scope_key TEXT NOT NULL,
          expected_count INTEGER NOT NULL DEFAULT 0,
          observed_count INTEGER NOT NULL DEFAULT 0,
          status TEXT NOT NULL CHECK(status IN ('isolated','needs_evidence','resolved')),
          evidence_required TEXT NOT NULL,
          note TEXT NOT NULL,
          created_at TEXT NOT NULL,
          UNIQUE(run_id,gap_type,scope_key)
        );
        CREATE INDEX IF NOT EXISTS ix_semantic_coverage_gap_run
          ON semantic_coverage_gap(run_id,status,gap_type);
        """
    )


def count(db: sqlite3.Connection, table: str, where: str = "", parameters: tuple[object, ...] = ()) -> int:
    suffix = f" WHERE {where}" if where else ""
    return int(db.execute(f"SELECT count(*) FROM {table}{suffix}", parameters).fetchone()[0])


def backlog_counts(path: pathlib.Path, metadata_path: pathlib.Path) -> tuple[int, int, int, dict[str, object]]:
    approval_pending = 0
    cleaning_pending = 0
    metadata_findings = 0
    detail: dict[str, object] = {}
    if path.exists():
        db = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        try:
            approval_pending = int(db.execute("SELECT count(*) FROM formal_approval_queue WHERE status='pending'").fetchone()[0])
            cleaning_pending = int(db.execute("SELECT count(*) FROM cleaning_run WHERE status IN ('draft','pending_approval')").fetchone()[0])
            detail["approval_status"] = [dict(row) for row in db.execute("SELECT status,count(*) AS count FROM formal_approval_queue GROUP BY status ORDER BY status").fetchall()]
            detail["cleaning_status"] = [dict(row) for row in db.execute("SELECT status,count(*) AS count FROM cleaning_run GROUP BY status ORDER BY status").fetchall()]
        finally:
            db.close()
    if metadata_path.exists():
        db = sqlite3.connect(f"file:{metadata_path.resolve()}?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        try:
            metadata_findings = int(db.execute("SELECT count(*) FROM metadata_validation_findings").fetchone()[0])
            detail["metadata_findings"] = [dict(row) for row in db.execute("SELECT finding_type,count(*) AS count FROM metadata_validation_findings GROUP BY finding_type ORDER BY finding_type").fetchall()]
        finally:
            db.close()
    return approval_pending, cleaning_pending, metadata_findings, detail


def build(target_path: pathlib.Path, workflow_path: pathlib.Path = DEFAULT_WORKFLOW, metadata_path: pathlib.Path = DEFAULT_METADATA) -> dict[str, object]:
    db = sqlite3.connect(str(target_path), timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout=30000")
    ensure_schema(db)
    created = now()

    registered = [dict(row) for row in db.execute(
        "SELECT event_type,subject_type,affects_state,review_status FROM ontology_event_type WHERE status='active' ORDER BY event_type"
    ).fetchall()]
    observed = {
        str(row[0]): int(row[1])
        for row in db.execute(
            "SELECT event_type,count(*) FROM semantic_event WHERE status IN ('observed','accepted') GROUP BY event_type"
        ).fetchall()
    }
    source_systems = int(db.execute("SELECT count(*) FROM source_system WHERE status='active'").fetchone()[0]) if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='source_system'").fetchone() else 0
    event_count = count(db, "semantic_event", "status IN ('observed','accepted')")
    source_fact_count = count(db, "semantic_fact", "fact_type IN ('observation_event','defect_event','work_order_event') AND status IN ('observed','accepted')")
    subject_count = int(db.execute("SELECT count(DISTINCT subject_type||':'||subject_key) FROM semantic_event WHERE status IN ('observed','accepted')").fetchone()[0])
    fact_count = count(db, "semantic_fact", "status<>'retracted'")
    derived_fact_count = count(db, "semantic_fact", "status='derived'")
    risk_fact_count = count(db, "semantic_fact", "fact_type='risk_assessment' AND status IN ('derived','accepted')")
    decision_count = count(db, "semantic_rule_decision", "status IN ('accepted','proposed','needs_review')")
    action_plan_count = count(db, "semantic_action_plan")
    approval_pending, cleaning_pending, metadata_findings, backlog_detail = backlog_counts(workflow_path, metadata_path)

    governance = None
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='semantic_governance_quality_run'").fetchone():
        row = db.execute("SELECT * FROM semantic_governance_quality_run ORDER BY created_at DESC LIMIT 1").fetchone()
        governance = dict(row) if row else None

    gaps: list[dict[str, object]] = []
    for item in registered:
        event_type = str(item["event_type"])
        seen = int(observed.get(event_type, 0))
        if seen == 0:
            gaps.append({
                "gap_type": "event_type_not_observed",
                "scope_key": event_type,
                "expected_count": 1,
                "observed_count": 0,
                "status": "needs_evidence",
                "evidence_required": f"{item['subject_type']} 对应源表记录、来源行和有效时间",
                "note": "注册表已定义，但当前本地快照没有该事件的可接受证据；不虚构事件。",
            })
    missing_time = count(db, "semantic_event", "status IN ('observed','accepted') AND (occurred_at IS NULL OR trim(occurred_at)='')")
    if missing_time:
        gaps.append({
            "gap_type": "event_time_missing",
            "scope_key": "semantic_event",
            "expected_count": event_count,
            "observed_count": event_count - missing_time,
            "status": "needs_evidence",
            "evidence_required": "源事件发生时间或可审计的记录时间",
            "note": f"有 {missing_time} 条事件缺少发生时间，不能进入时间顺序状态迁移。",
        })
    unlinked = count(db, "semantic_event", "status IN ('observed','accepted') AND identity_status<>'accepted'")
    if unlinked:
        gaps.append({
            "gap_type": "event_identity_not_accepted",
            "scope_key": "semantic_event",
            "expected_count": event_count,
            "observed_count": event_count - unlinked,
            "status": "isolated",
            "evidence_required": "设备、位置或业务编号的明确映射证据",
            "note": f"有 {unlinked} 条事件身份未达到 accepted，继续隔离。",
        })
    latest_fact_builder = db.execute("SELECT * FROM semantic_fact_builder_run ORDER BY created_at DESC LIMIT 1").fetchone() if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='semantic_fact_builder_run'").fetchone() else None
    if latest_fact_builder and int(latest_fact_builder["input_event_count"] or 0) > 0 and int(latest_fact_builder["risk_assessment_count"] or 0) == 0:
        gaps.append({
            "gap_type": "risk_evidence_absent",
            "scope_key": "risk_assessment",
            "expected_count": int(latest_fact_builder["input_event_count"] or 0),
            "observed_count": 0,
            "status": "needs_evidence",
            "evidence_required": "明确数值+单位、有效时间或重复缺陷证据",
            "note": "当前快照没有足够风险证据，风险规则不生成行动。",
        })
    if approval_pending:
        gaps.append({
            "gap_type": "approval_backlog",
            "scope_key": "formal_approval_queue",
            "expected_count": approval_pending,
            "observed_count": 0,
            "status": "isolated",
            "evidence_required": "按 replay_id/site 分层抽查和人工审批",
            "note": "已进入可追踪积压，不自动审批、不删除。",
        })
    if metadata_findings:
        gaps.append({
            "gap_type": "metadata_backlog",
            "scope_key": "metadata_validation_findings",
            "expected_count": metadata_findings,
            "observed_count": 0,
            "status": "isolated",
            "evidence_required": "按 finding_type 和源表补齐结构证据",
            "note": "元数据问题继续留在版本化结果层，不修改 GRP 源表。",
        })
    if governance and governance.get("status") != "completed":
        gaps.append({
            "gap_type": "semantic_governance_isolation",
            "scope_key": "semantic_governance_quality_run",
            "expected_count": int(governance.get("isolated_count") or 0),
            "observed_count": 0,
            "status": "isolated",
            "evidence_required": "补齐位置证据、跨事件因果证据或解决身份冲突",
            "note": "治理契约已执行；缺少证据的关系保持隔离，不自动合并或推断。",
        })

    run_id = sid("SCVR", COVERAGE_VERSION, created)
    report = {
        "runId": run_id,
        "coverageVersion": COVERAGE_VERSION,
        "sourceSystems": source_systems,
        "registeredEventTypes": registered,
        "observedEventTypes": observed,
        "eventCount": event_count,
        "sourceFactCount": source_fact_count,
        "subjectCount": subject_count,
        "factCount": fact_count,
        "derivedFactCount": derived_fact_count,
        "riskFactCount": risk_fact_count,
        "decisionCount": decision_count,
        "actionPlanCount": action_plan_count,
        "approvalPendingCount": approval_pending,
        "cleaningPendingCount": cleaning_pending,
        "metadataFindingCount": metadata_findings,
        "backlogDetail": backlog_detail,
        "governance": governance,
        "gaps": gaps,
        "policy": "有证据才推进；无证据进入 needs_evidence/isolated；本地闭环不代表源系统已执行。",
        "sourceWrite": False,
        "formalPublication": False,
    }
    status = "completed" if not gaps else "partial"
    db.execute(
        """INSERT INTO semantic_coverage_run(
          run_id,coverage_version,source_system_count,source_fact_count,event_count,subject_count,
          event_type_registered_count,event_type_observed_count,event_type_missing_count,fact_count,
          derived_fact_count,risk_fact_count,decision_count,action_plan_count,approval_pending_count,
          metadata_finding_count,gap_count,status,report_json,source_write,formal_publication,created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,0,?)""",
        (run_id, COVERAGE_VERSION, source_systems, source_fact_count, event_count, subject_count,
         len(registered), len([item for item in registered if observed.get(str(item["event_type"]), 0)]),
         len([item for item in registered if not observed.get(str(item["event_type"]), 0)]), fact_count,
         derived_fact_count, risk_fact_count, decision_count, action_plan_count, approval_pending,
         metadata_findings, len(gaps), status, json.dumps(report, ensure_ascii=False), created),
    )
    for gap in gaps:
        db.execute(
            """INSERT INTO semantic_coverage_gap(
              gap_id,run_id,gap_type,scope_key,expected_count,observed_count,status,
              evidence_required,note,created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (sid("SCG", run_id, gap["gap_type"], gap["scope_key"]), run_id, gap["gap_type"], gap["scope_key"],
             gap["expected_count"], gap["observed_count"], gap["status"], gap["evidence_required"], gap["note"], created),
        )
    db.commit()
    db.close()
    return {**report, "status": status, "gapCount": len(gaps)}


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the local semantic coverage and closure report")
    parser.add_argument("--target-db", type=pathlib.Path, default=DEFAULT_TARGET)
    parser.add_argument("--workflow-db", type=pathlib.Path, default=DEFAULT_WORKFLOW)
    parser.add_argument("--metadata-db", type=pathlib.Path, default=DEFAULT_METADATA)
    args = parser.parse_args()
    print(json.dumps(build(args.target_db.resolve(), args.workflow_db.resolve(), args.metadata_db.resolve()), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
