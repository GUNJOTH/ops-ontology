"""Build deterministic measurement, history, trend, risk and defect facts.

The builder consumes only local accepted/observed facts.  Every output keeps
its input fact IDs and rule version.  Missing values, units, timestamps or
business evidence are ignored or routed to review; no source system is
written and no measurement is invented.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import pathlib
import re
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from pipeline.contracts import connect_local


ROOT = pathlib.Path(__file__).resolve().parent
DEFAULT_TARGET = ROOT / "data" / "unified_semantics.sqlite3"
BUILDER_VERSION = "fact-builders-v1"


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sid(prefix: str, *parts: object) -> str:
    raw = "|".join("" if part is None else str(part) for part in parts)
    return f"{prefix}-{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:24]}"


def derived_snapshot_id(items: list[object]) -> str | None:
    """Return a deterministic snapshot reference for a derived fact.

    A derived fact must retain the snapshot evidence of its inputs.  A single
    input snapshot is preserved verbatim; a multi-snapshot derivation gets a
    stable derived reference so replay and provenance can still identify the
    exact input snapshot set without pretending it came from one source row.
    """
    snapshots: set[str] = set()
    for item in items:
        value: object = None
        if isinstance(item, sqlite3.Row):
            value = item["source_snapshot_id"]
        elif isinstance(item, dict):
            value = item.get("source_snapshot_id")
        elif isinstance(item, str):
            value = item
        text = str(value or "").strip()
        if text:
            snapshots.add(text)
    if not snapshots:
        return None
    if len(snapshots) == 1:
        return next(iter(snapshots))
    return "derived:" + hashlib.sha256("|".join(sorted(snapshots)).encode("utf-8")).hexdigest()[:24]


def parse_time(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def as_json(value: object) -> dict[str, object]:
    try:
        parsed = json.loads(value or "{}") if isinstance(value, str) else value
    except (TypeError, json.JSONDecodeError):
        parsed = {}
    return parsed if isinstance(parsed, dict) else {}


def norm_text(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip()).casefold()


def safe_float(value: object, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) else default


def ensure_schema(db: sqlite3.Connection, created: str) -> None:
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS semantic_fact_builder_rule (
          rule_key TEXT PRIMARY KEY,
          rule_version TEXT NOT NULL,
          title TEXT NOT NULL,
          output_fact_type TEXT NOT NULL,
          parameters_json TEXT NOT NULL,
          status TEXT NOT NULL CHECK(status IN ('enabled','disabled','blocked')),
          source_write INTEGER NOT NULL CHECK(source_write=0),
          formal_publication INTEGER NOT NULL CHECK(formal_publication=0),
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS semantic_fact_builder_run (
          run_id TEXT PRIMARY KEY,
          builder_version TEXT NOT NULL,
          input_event_count INTEGER NOT NULL,
          measurement_count INTEGER NOT NULL,
          history_stat_count INTEGER NOT NULL,
          trend_count INTEGER NOT NULL,
          repeated_defect_count INTEGER NOT NULL,
          severe_defect_count INTEGER NOT NULL,
          risk_assessment_count INTEGER NOT NULL,
          review_count INTEGER NOT NULL,
          status TEXT NOT NULL CHECK(status IN ('completed','needs_review','blocked')),
          source_write INTEGER NOT NULL CHECK(source_write=0),
          formal_publication INTEGER NOT NULL CHECK(formal_publication=0),
          created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_fact_builder_rule_status
          ON semantic_fact_builder_rule(status,output_fact_type);
        CREATE INDEX IF NOT EXISTS ix_fact_builder_run_created
          ON semantic_fact_builder_run(created_at);
        """
    )
    rules = (
        ("measurement.temperature", "temperature-measurement-v1", "显式温度测量事实", "measurement", {"metric": "temperature", "unitRequired": True}),
        ("history.statistic", "history-statistic-v1", "测量历史统计事实", "history_stat", {"minimumCount": 1}),
        ("trend.measurement", "measurement-trend-v1", "同一指标时间趋势事实", "trend", {"minimumCount": 2}),
        ("defect.repeated", "repeated-defect-v1", "时间窗内重复缺陷事实", "repeated_defect", {"windowDays": 30, "minimumCount": 2}),
        ("defect.severe", "severe-defect-v1", "显式严重缺陷事实", "severe_defect", {"acceptedValues": ["high", "severe", "urgent", "critical"]}),
        ("risk.assessment", "risk-assessment-v1", "由测量和缺陷证据计算风险事实", "risk_assessment", {"temperatureThreshold": 80, "temperatureUnit": "C", "acceptedTemperatureUnits": ["C", "℃", "°C", "degC"], "highRiskScore": 4}),
    )
    for key, version, title, output_type, parameters in rules:
        db.execute(
            """INSERT INTO semantic_fact_builder_rule(
              rule_key,rule_version,title,output_fact_type,parameters_json,status,
              source_write,formal_publication,created_at,updated_at
            ) VALUES (?,?,?,?,?,'enabled',0,0,?,?)
            ON CONFLICT(rule_key) DO UPDATE SET
              rule_version=excluded.rule_version,title=excluded.title,
              output_fact_type=excluded.output_fact_type,parameters_json=excluded.parameters_json,
              updated_at=excluded.updated_at""",
            (key, version, title, output_type, json.dumps(parameters, ensure_ascii=False), created, created),
        )


def rule_config(db: sqlite3.Connection, key: str) -> tuple[str, dict[str, object]] | None:
    row = db.execute("SELECT rule_version,parameters_json FROM semantic_fact_builder_rule WHERE rule_key=? AND status='enabled'", (key,)).fetchone()
    if row is None:
        return None
    return str(row["rule_version"]), as_json(row["parameters_json"])


def upsert_fact(
    db: sqlite3.Connection,
    fact_id: str,
    fact_type: str,
    subject_type: str,
    subject_key: str,
    predicate: str,
    value: dict[str, object],
    source_fact_id: str,
    source_snapshot_id: str | None,
    confidence: float,
    observed_at: str | None,
    created: str,
) -> bool:
    existed = db.execute("SELECT 1 FROM semantic_fact WHERE fact_id=?", (fact_id,)).fetchone() is not None
    db.execute(
        """INSERT INTO semantic_fact(
          fact_id,fact_type,subject_type,subject_key,predicate,value_json,unit,
          source_schema,source_table,source_row_id,source_snapshot_id,status,
          confidence,observed_at,created_at
        ) VALUES (?,?,?,?,?,?,NULL,'LOCAL_SEMANTIC','semantic_fact_builder',?,?,?,?,?,?)
        ON CONFLICT(fact_id) DO UPDATE SET
          value_json=excluded.value_json,source_row_id=excluded.source_row_id,
          source_snapshot_id=excluded.source_snapshot_id,status=excluded.status,
          confidence=excluded.confidence,observed_at=excluded.observed_at,created_at=excluded.created_at""",
        (fact_id, fact_type, subject_type, subject_key, predicate, json.dumps(value, ensure_ascii=False), source_fact_id, source_snapshot_id, "derived", confidence, observed_at, created),
    )
    return not existed


def add_derivation(db: sqlite3.Connection, output_fact_id: str, rule_key: str, rule_version: str, input_ids: list[str], explanation: str, created: str) -> None:
    derivation_id = sid("SFD", rule_version, output_fact_id)
    db.execute(
        """INSERT OR REPLACE INTO semantic_fact_derivation(
          derivation_id,output_fact_id,rule_asset_id,rule_version_id,
          input_fact_ids_json,input_context_json,decision_id,explanation,
          constraint_results_json,status,created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,'accepted',?)""",
        (derivation_id, output_fact_id, f"SBR:{rule_key}", f"SBRV:{rule_version}", json.dumps(input_ids), json.dumps({"builder": BUILDER_VERSION}), None, explanation, json.dumps({"source_write": "pass", "formal_publication": "pass"}), created),
    )


def source_event(fact: sqlite3.Row) -> dict[str, object]:
    value = as_json(fact["value_json"])
    item = value.get("source_event")
    return item if isinstance(item, dict) else {}


def extract_measurements(facts: list[sqlite3.Row], rule_version: str, parameters: dict[str, object], db: sqlite3.Connection, created: str) -> tuple[list[dict[str, object]], int, int]:
    measurements: list[dict[str, object]] = []
    review = 0
    created_count = 0
    for fact in facts:
        event = source_event(fact)
        payload = as_json(event.get("payload"))
        merged = {**event, **payload}
        candidates: list[tuple[str, object, object]] = []
        for key in ("temperature", "temperature_c", "temperatureC", "measurement_value", "measurementValue", "numeric_value", "numericValue"):
            if key in merged:
                candidates.append(("temperature" if "temperature" in key.lower() else str(merged.get("metric") or merged.get("parameter") or "value"), merged[key], merged.get("unit") or merged.get("temperature_unit") or merged.get("temperatureUnit")))
        raw_measurements = merged.get("measurements")
        if isinstance(raw_measurements, list):
            for item in raw_measurements:
                if isinstance(item, dict):
                    candidates.append((str(item.get("metric") or item.get("name") or ""), item.get("value"), item.get("unit")))
        for metric, raw_value, raw_unit in candidates:
            try:
                numeric = float(raw_value)
            except (TypeError, ValueError):
                review += 1
                continue
            if not math.isfinite(numeric) or not str(raw_unit or "").strip():
                review += 1
                continue
            event_time = str(event.get("event_time") or fact["observed_at"] or fact["created_at"] or "") or None
            output_id = sid("FACT", "measurement", fact["fact_id"], metric, numeric, raw_unit)
            value = {"metric": metric, "value": numeric, "unit": str(raw_unit).strip(), "derived_from_fact_id": fact["fact_id"], "fact_class": "MeasurementFact"}
            if upsert_fact(db, output_id, "measurement", fact["subject_type"], fact["subject_key"], "has_measurement", value, fact["fact_id"], fact["source_snapshot_id"], min(1.0, max(0.0, safe_float(fact["confidence"]))), event_time, created):
                created_count += 1
            add_derivation(db, output_id, "measurement.temperature", rule_version, [fact["fact_id"]], "仅使用来源中明确的数值和单位生成测量事实", created)
            measurements.append({"fact_id": output_id, "input_fact_id": fact["fact_id"], "source_snapshot_id": fact["source_snapshot_id"], "subject_type": fact["subject_type"], "subject_key": fact["subject_key"], "metric": metric, "value": numeric, "unit": str(raw_unit).strip(), "event_time": parse_time(event_time), "observed_at": event_time})
    return measurements, review, created_count


def build(target_path: pathlib.Path) -> dict[str, object]:
    db = connect_local(target_path, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA busy_timeout=30000")
    created = now()
    ensure_schema(db, created)
    measurement_rule = rule_config(db, "measurement.temperature")
    if measurement_rule is None:
        db.commit()
        db.close()
        return {"status": "blocked", "note": "measurement rule is not enabled", "source_write": False, "formal_publication": False}
    measurement_version, measurement_parameters = measurement_rule
    source_facts = db.execute(
        "SELECT * FROM semantic_fact WHERE fact_type='observation_event' AND status IN ('observed','accepted') ORDER BY fact_id"
    ).fetchall()
    measurements, review_count, created_measurement_count = extract_measurements(source_facts, measurement_version, measurement_parameters, db, created)

    groups: dict[tuple[str, str, str, str], list[dict[str, object]]] = defaultdict(list)
    for item in measurements:
        groups[(item["subject_type"], item["subject_key"], item["metric"], item["unit"])].append(item)
    counts = {"measurement": created_measurement_count, "history_stat": 0, "trend": 0, "repeated_defect": 0, "severe_defect": 0, "risk_assessment": 0}
    for group_key, items in groups.items():
        subject_type, subject_key, metric, unit = group_key
        ordered = sorted(items, key=lambda item: (item["event_time"] or datetime.max.replace(tzinfo=timezone.utc), item["fact_id"]))
        values = [float(item["value"]) for item in ordered]
        stat_rule = rule_config(db, "history.statistic")
        if stat_rule and len(values) >= int(stat_rule[1].get("minimumCount") or 1):
            version = stat_rule[0]
            output_id = sid("FACT", "history_stat", subject_key, metric, unit, *(item["fact_id"] for item in ordered))
            value = {"metric": metric, "unit": unit, "count": len(values), "min": min(values), "max": max(values), "avg": sum(values) / len(values), "input_fact_ids": [item["fact_id"] for item in ordered], "fact_class": "HistoryStatisticFact"}
            created_output = upsert_fact(db, output_id, "history_stat", subject_type, subject_key, "has_history_statistic", value, ordered[-1]["fact_id"], derived_snapshot_id(ordered), 1.0, ordered[-1]["observed_at"], created)
            add_derivation(db, output_id, "history.statistic", version, [item["fact_id"] for item in ordered], "由同一设备、指标和单位的测量事实计算统计值", created)
            counts["history_stat"] += int(created_output)
        trend_rule = rule_config(db, "trend.measurement")
        if trend_rule and len(ordered) >= int(trend_rule[1].get("minimumCount") or 2) and ordered[0]["event_time"] and ordered[-1]["event_time"]:
            version = trend_rule[0]
            delta = values[-1] - values[0]
            direction = "up" if delta > 0 else "down" if delta < 0 else "flat"
            output_id = sid("FACT", "trend", subject_key, metric, unit, *(item["fact_id"] for item in ordered))
            value = {"metric": metric, "unit": unit, "from": values[0], "to": values[-1], "delta": delta, "direction": direction, "from_at": ordered[0]["observed_at"], "to_at": ordered[-1]["observed_at"], "input_fact_ids": [item["fact_id"] for item in ordered], "fact_class": "TrendFact"}
            created_output = upsert_fact(db, output_id, "trend", subject_type, subject_key, "has_measurement_trend", value, ordered[-1]["fact_id"], derived_snapshot_id(ordered), 1.0, ordered[-1]["observed_at"], created)
            add_derivation(db, output_id, "trend.measurement", version, [item["fact_id"] for item in ordered], "按有效事件时间计算同一指标的趋势", created)
            counts["trend"] += int(created_output)

    defect_facts = db.execute("SELECT * FROM semantic_fact WHERE fact_type='defect_event' AND status IN ('observed','accepted') ORDER BY fact_id").fetchall()
    defects: dict[tuple[str, str], list[dict[str, object]]] = defaultdict(list)
    severe_rule = rule_config(db, "defect.severe")
    repeated_rule = rule_config(db, "defect.repeated")
    for fact in defect_facts:
        event = source_event(fact)
        event_time_text = str(event.get("event_time") or fact["observed_at"] or fact["created_at"] or "") or None
        item = {"fact": fact, "event": event, "time": parse_time(event_time_text), "description": norm_text(event.get("description") or event.get("fault_desc") or event.get("faultDescription")), "severity": norm_text(event.get("severity") or event.get("risk_level") or event.get("riskLevel"))}
        defects[(fact["subject_type"], fact["subject_key"])].append(item)
        if severe_rule and item["severity"] in {str(value).casefold() for value in severe_rule[1].get("acceptedValues", [])}:
            output_id = sid("FACT", "severe_defect", fact["fact_id"])
            value = {"severity": item["severity"], "derived_from_fact_id": fact["fact_id"], "fact_class": "SevereDefectFact"}
            created_output = upsert_fact(db, output_id, "severe_defect", fact["subject_type"], fact["subject_key"], "has_severe_defect", value, fact["fact_id"], fact["source_snapshot_id"], min(1.0, max(0.0, safe_float(fact["confidence"]))), event_time_text, created)
            add_derivation(db, output_id, "defect.severe", severe_rule[0], [fact["fact_id"]], "仅依据来源明确的严重程度字段", created)
            counts["severe_defect"] += int(created_output)

    repeated_inputs: dict[tuple[str, str], list[str]] = defaultdict(list)
    if repeated_rule:
        window_days = int(repeated_rule[1].get("windowDays") or 30)
        minimum = int(repeated_rule[1].get("minimumCount") or 2)
        for (subject_type, subject_key), items in defects.items():
            by_description: dict[str, list[dict[str, object]]] = defaultdict(list)
            for item in items:
                if item["description"] and item["time"]:
                    by_description[item["description"]].append(item)
            for description, same in by_description.items():
                same.sort(key=lambda item: item["time"])
                for start in range(len(same)):
                    window = [item for item in same[start:] if item["time"] - same[start]["time"] <= timedelta(days=window_days)]
                    if len(window) >= minimum:
                        input_ids = [item["fact"]["fact_id"] for item in window]
                        output_id = sid("FACT", "repeated_defect", subject_key, description, *input_ids)
                        value = {"description_key": description, "window_days": window_days, "count": len(window), "event_times": [item["time"].isoformat() for item in window], "input_fact_ids": input_ids, "fact_class": "RepeatedDefectFact"}
                        created_output = upsert_fact(db, output_id, "repeated_defect", subject_type, subject_key, "has_repeated_defect", value, input_ids[-1], window[-1]["fact"]["source_snapshot_id"], 1.0, window[-1]["time"].isoformat(), created)
                        add_derivation(db, output_id, "defect.repeated", repeated_rule[0], input_ids, "同一设备、相同描述在配置时间窗内重复出现", created)
                        counts["repeated_defect"] += int(created_output)
                        repeated_inputs[(subject_type, subject_key)].append(output_id)
                        break

    risk_rule = rule_config(db, "risk.assessment")
    if risk_rule:
        threshold = safe_float(risk_rule[1].get("temperatureThreshold"), 80.0)
        high_score = int(safe_float(risk_rule[1].get("highRiskScore"), 4.0))
        configured_units = risk_rule[1].get("acceptedTemperatureUnits") or []
        if not configured_units and risk_rule[1].get("temperatureUnit"):
            configured_units = [risk_rule[1]["temperatureUnit"]]
        accepted_temperature_units = {
            str(value).casefold() for value in configured_units if str(value).strip()
        }
        evidence_by_subject: dict[tuple[str, str], list[tuple[str, str]]] = defaultdict(list)
        for item in measurements:
            if item["metric"].casefold() == "temperature" and str(item["unit"]).casefold() in accepted_temperature_units and safe_float(item["value"], float("-inf")) >= threshold:
                evidence_by_subject[(item["subject_type"], item["subject_key"])].append((item["fact_id"], f"temperature>={threshold}{item['unit']}"))
        for key, input_ids in repeated_inputs.items():
            evidence_by_subject[key].append((sid("FACT", "repeated_defect", *input_ids), "repeated_defect"))
        for key, evidence in evidence_by_subject.items():
            subject_type, subject_key = key
            ids = [item[0] for item in evidence]
            score = min(5, max(high_score if any("temperature" in item[1] for item in evidence) else 3, len(evidence) + 2))
            output_id = sid("FACT", "risk_assessment", subject_key, *ids)
            value = {"risk_score": score, "risk_flags": [item[1] for item in evidence], "input_fact_ids": ids, "fact_class": "RiskAssessmentFact"}
            input_rows = db.execute(
                f"SELECT source_snapshot_id FROM semantic_fact WHERE fact_id IN ({','.join('?' for _ in ids)})",
                ids,
            ).fetchall()
            created_output = upsert_fact(db, output_id, "risk_assessment", subject_type, subject_key, "has_risk_assessment", value, ids[-1], derived_snapshot_id(list(input_rows)), 1.0, created, created)
            add_derivation(db, output_id, "risk.assessment", risk_rule[0], ids, "由显式测量阈值或重复缺陷事实计算风险分数", created)
            counts["risk_assessment"] += int(created_output)

    status = "needs_review" if review_count else "completed"
    run_id = sid("SFBR", BUILDER_VERSION, created)
    db.execute(
        """INSERT INTO semantic_fact_builder_run(
          run_id,builder_version,input_event_count,measurement_count,history_stat_count,
          trend_count,repeated_defect_count,severe_defect_count,risk_assessment_count,
          review_count,status,source_write,formal_publication,created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,0,0,?)""",
        (run_id, BUILDER_VERSION, len(source_facts) + len(defect_facts), counts["measurement"], counts["history_stat"], counts["trend"], counts["repeated_defect"], counts["severe_defect"], counts["risk_assessment"], review_count, status, created),
    )
    db.commit()
    db.close()
    return {"run_id": run_id, "builder_version": BUILDER_VERSION, "input_event_count": len(source_facts) + len(defect_facts), **counts, "review_count": review_count, "status": status, "source_write": False, "formal_publication": False}


def main() -> None:
    parser = argparse.ArgumentParser(description="Build deterministic semantic facts locally")
    parser.add_argument("--target-db", type=pathlib.Path, default=DEFAULT_TARGET)
    args = parser.parse_args()
    print(json.dumps(build(args.target_db.resolve()), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
