"""Materialize canonical defect states into transition history and current state.

This engine consumes only approved ``canonical_defect_state`` facts.  It never
interprets raw status values, invents a transition, or writes an upstream
system.  A missing time order is treated as reviewable evidence instead of
silently replacing the current state.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone


ROOT = pathlib.Path(__file__).resolve().parent
DEFAULT_TARGET = ROOT / "data" / "unified_semantics.sqlite3"
RULE_ASSET_ID = "SBR:state-transition.defect-canonical"
RULE_VERSION_ID = "SBRV:state-transition.defect-canonical-v2"
STATE_MACHINE_ID = "SM:DEFECT:v1"


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sid(prefix: str, *parts: object) -> str:
    raw = "|".join("" if part is None else str(part) for part in parts)
    return f"{prefix}-{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:24]}"


def ensure_schema(db: sqlite3.Connection) -> None:
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS semantic_state_transition (
          transition_id TEXT PRIMARY KEY,
          subject_type TEXT NOT NULL,
          subject_key TEXT NOT NULL,
          state_domain TEXT NOT NULL,
          from_state TEXT,
          to_state TEXT NOT NULL,
          transition_type TEXT NOT NULL CHECK (transition_type IN ('initial','change','replay')),
          trigger_fact_id TEXT NOT NULL REFERENCES semantic_fact(fact_id),
          event_id TEXT REFERENCES semantic_event(event_id),
          rule_asset_id TEXT NOT NULL,
          rule_version_id TEXT NOT NULL,
          effective_at TEXT,
          source_snapshot_id TEXT,
          evidence_json TEXT NOT NULL,
          confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
          status TEXT NOT NULL CHECK (status IN ('accepted','needs_review','rejected')),
          created_at TEXT NOT NULL,
          UNIQUE(subject_type,subject_key,state_domain,trigger_fact_id)
        );
        CREATE TABLE IF NOT EXISTS semantic_current_state (
          subject_type TEXT NOT NULL,
          subject_key TEXT NOT NULL,
          state_domain TEXT NOT NULL,
          current_state TEXT NOT NULL,
          display_name TEXT NOT NULL,
          source_fact_id TEXT NOT NULL REFERENCES semantic_fact(fact_id),
          transition_id TEXT NOT NULL REFERENCES semantic_state_transition(transition_id),
          effective_at TEXT,
          source_snapshot_id TEXT,
          state_version INTEGER NOT NULL,
          status TEXT NOT NULL CHECK (status IN ('current','needs_review','retracted')),
          updated_at TEXT NOT NULL,
          PRIMARY KEY(subject_type,subject_key,state_domain)
        );
        CREATE TABLE IF NOT EXISTS semantic_state_transition_run (
          run_id TEXT PRIMARY KEY,
          input_fact_count INTEGER NOT NULL,
          accepted_transition_count INTEGER NOT NULL,
          review_transition_count INTEGER NOT NULL,
          current_state_count INTEGER NOT NULL,
          status TEXT NOT NULL CHECK (status IN ('completed','needs_review','blocked')),
          source_write INTEGER NOT NULL CHECK (source_write=0),
          formal_publication INTEGER NOT NULL CHECK (formal_publication=0),
          created_at TEXT NOT NULL,
          replay_version TEXT NOT NULL DEFAULT 'state-replay-v2',
          scope_subject_type TEXT,
          scope_subject_key TEXT,
          subject_count INTEGER NOT NULL DEFAULT 0,
          late_arrival_count INTEGER NOT NULL DEFAULT 0,
          difference_count INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS semantic_state_replay_diff (
          diff_id TEXT PRIMARY KEY,
          replay_run_id TEXT NOT NULL REFERENCES semantic_state_transition_run(run_id),
          subject_type TEXT NOT NULL,
          subject_key TEXT NOT NULL,
          state_domain TEXT NOT NULL,
          difference_type TEXT NOT NULL CHECK (difference_type IN ('added','removed','state_changed','effective_time_changed','review_status_changed')),
          previous_state TEXT,
          replay_state TEXT,
          previous_effective_at TEXT,
          replay_effective_at TEXT,
          evidence_json TEXT NOT NULL,
          status TEXT NOT NULL CHECK (status IN ('reported','needs_review')),
          created_at TEXT NOT NULL,
          UNIQUE(replay_run_id,subject_type,subject_key,state_domain,difference_type)
        );
        CREATE INDEX IF NOT EXISTS ix_semantic_state_replay_diff_run
          ON semantic_state_replay_diff(replay_run_id,difference_type,status);
        CREATE INDEX IF NOT EXISTS ix_semantic_state_transition_subject
          ON semantic_state_transition(subject_type,subject_key,state_domain,effective_at);
        CREATE INDEX IF NOT EXISTS ix_semantic_current_state_domain
          ON semantic_current_state(state_domain,current_state,status);
        """
    )
    columns = {row[1] for row in db.execute("PRAGMA table_info(semantic_state_transition)")}
    if "event_id" not in columns:
        db.execute("ALTER TABLE semantic_state_transition ADD COLUMN event_id TEXT")
    additive_transition = {
        "transition_rule_id": "TEXT",
        "order_key": "TEXT",
        "late_arrival": "INTEGER NOT NULL DEFAULT 0",
        "guard_status": "TEXT NOT NULL DEFAULT 'not_evaluated'",
        "replay_run_id": "TEXT",
    }
    for column, definition in additive_transition.items():
        if column not in columns:
            db.execute(f"ALTER TABLE semantic_state_transition ADD COLUMN {column} {definition}")
    run_columns = {row[1] for row in db.execute("PRAGMA table_info(semantic_state_transition_run)")}
    additive_run = {
        "replay_version": "TEXT NOT NULL DEFAULT 'state-replay-v2'",
        "scope_subject_type": "TEXT",
        "scope_subject_key": "TEXT",
        "subject_count": "INTEGER NOT NULL DEFAULT 0",
        "late_arrival_count": "INTEGER NOT NULL DEFAULT 0",
        "difference_count": "INTEGER NOT NULL DEFAULT 0",
    }
    for column, definition in additive_run.items():
        if column not in run_columns:
            db.execute(f"ALTER TABLE semantic_state_transition_run ADD COLUMN {column} {definition}")


def parse_time(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def source_event_for_fact(db: sqlite3.Connection, fact: sqlite3.Row) -> tuple[sqlite3.Row | None, dict[str, object]]:
    try:
        value = json.loads(fact["value_json"] or "{}")
    except (TypeError, json.JSONDecodeError):
        value = {}
    source_fact_id = value.get("derived_from_fact_id") if isinstance(value, dict) else None
    source_fact = db.execute("SELECT * FROM semantic_fact WHERE fact_id=?", (source_fact_id,)).fetchone() if source_fact_id else None
    if source_fact is not None and source_fact["fact_type"] == "defect_status":
        try:
            status_value = json.loads(source_fact["value_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            status_value = {}
        event_fact_id = status_value.get("event_fact_id") if isinstance(status_value, dict) else None
        event_fact = db.execute("SELECT * FROM semantic_fact WHERE fact_id=?", (event_fact_id,)).fetchone() if event_fact_id else None
        if event_fact is not None:
            source_fact = event_fact
    source_event: dict[str, object] = {}
    if source_fact is not None:
        try:
            source_value = json.loads(source_fact["value_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            source_value = {}
        if isinstance(source_value, dict) and isinstance(source_value.get("source_event"), dict):
            source_event = source_value["source_event"]
    return source_fact, source_event


def semantic_event_for_source(db: sqlite3.Connection, source_fact: sqlite3.Row | None) -> sqlite3.Row | None:
    if source_fact is None:
        return None
    return db.execute(
        """
        SELECT * FROM semantic_event
        WHERE source_schema=? AND source_table=? AND source_row_id=?
          AND (source_snapshot_id=? OR source_snapshot_id IS NULL)
        ORDER BY status='accepted' DESC, occurred_at DESC, event_id
        LIMIT 1
        """,
        (source_fact["source_schema"], source_fact["source_table"], source_fact["source_row_id"], source_fact["source_snapshot_id"]),
    ).fetchone()


def load_transition_rules(db: sqlite3.Connection) -> list[sqlite3.Row]:
    return db.execute(
        """
        SELECT * FROM ontology_transition_rule
        WHERE machine_id=? AND status='active' AND review_status='approved'
        ORDER BY priority DESC, transition_rule_id
        """,
        (STATE_MACHINE_ID,),
    ).fetchall()


def event_type_for(event: sqlite3.Row | None, source_event: dict[str, object]) -> str | None:
    if event is not None and str(event["event_type"] or "").strip():
        return str(event["event_type"]).strip()
    raw = str(source_event.get("event_type") or "").strip().lower()
    return {
        "defect_created": "DefectCreatedEvent",
        "defect_accepted": "DefectAcceptedEvent",
        "defect_processing": "DefectProcessingEvent",
        "resolution": "ResolutionEvent",
        "defect_acceptance": "DefectAcceptanceEvent",
    }.get(raw, raw or None)


def context_for(value: dict[str, object], source_event: dict[str, object], event: sqlite3.Row | None) -> dict[str, object]:
    context: dict[str, object] = {}
    for item in (value, source_event):
        if isinstance(item, dict):
            context.update(item)
    if event is not None:
        try:
            payload = json.loads(event["payload_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            payload = {}
        if isinstance(payload, dict):
            context.update(payload)
            if isinstance(payload.get("source_event"), dict):
                context.update(payload["source_event"])
    return context


def guard_result(guard_json: str, context: dict[str, object], event_type: str | None, from_state: str | None) -> tuple[bool, str]:
    try:
        guard = json.loads(guard_json or "{}")
    except json.JSONDecodeError:
        return False, "invalid_guard_json"
    if not isinstance(guard, dict) or not guard.get("requires"):
        return True, "not_required"
    requirement = str(guard.get("guard_key") or guard.get("requires") or "").strip().lower()
    legacy_aliases = {
        "team assigned": "team_assignment_present",
        "resolution evidence": "resolution_evidence_present",
        "acceptance_pass=true": "acceptance_pass",
        "restore event": "restore_event",
        "review/accept evidence": "review_accept_evidence",
    }
    requirement = legacy_aliases.get(requirement, requirement)
    if requirement == "team_assignment_present":
        ok = any(str(context.get(key) or "").strip() for key in ("team_id", "team_num", "teamId", "teamNum", "assigned_team"))
        return ok, "team_assignment_present" if ok else "team_assignment_missing"
    if requirement == "resolution_evidence_present":
        ok = event_type == "ResolutionEvent" or any(str(context.get(key) or "").strip() for key in ("resolution_id", "resolution_desc", "resolution_description", "resolutionId"))
        return ok, "resolution_evidence_present" if ok else "resolution_evidence_missing"
    if requirement == "acceptance_pass":
        raw = context.get("acceptance_pass", context.get("acceptancePass"))
        ok = raw is True or str(raw).strip().lower() in {"true", "1", "yes", "pass", "passed"}
        return ok, "acceptance_pass" if ok else "acceptance_pass_missing"
    if requirement == "restore_event":
        ok = from_state in {"CLOSED", "CANCELLED"} and event_type == "DefectProcessingEvent"
        return ok, "restore_event" if ok else "restore_event_missing"
    if requirement == "review_accept_evidence":
        raw_status = str(context.get("status") or context.get("raw_status") or "").lower()
        ok = event_type in {"DefectAcceptedEvent", "DefectAcceptanceEvent"} or any(token in raw_status for token in ("accept", "confirm", "已确认", "已接收"))
        return ok, "acceptance_evidence" if ok else "acceptance_evidence_missing"
    return False, "unknown_guard"


def choose_rule(
    rules: list[sqlite3.Row],
    from_state: str | None,
    event_type: str | None,
    to_state: str,
    context: dict[str, object],
) -> tuple[sqlite3.Row | None, bool, str]:
    candidates = [
        rule for rule in rules
        if (rule["from_state"] is None or rule["from_state"] == from_state)
        and rule["event_type"] == event_type
        and rule["to_state"] == to_state
    ]
    if not candidates:
        return None, False, "transition_rule_missing"
    for rule in candidates:
        ok, reason = guard_result(rule["guard_json"], context, event_type, from_state)
        if ok:
            return rule, True, reason
    return candidates[0], False, guard_result(candidates[0]["guard_json"], context, event_type, from_state)[1]


def replay(target_path: pathlib.Path, subject_type: str | None = None, subject_key: str | None = None) -> dict[str, object]:
    db = sqlite3.connect(str(target_path), timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA busy_timeout=30000")
    ensure_schema(db)
    db.commit()
    created = now()
    run_id = sid("SSTR", RULE_VERSION_ID, created, subject_type, subject_key)
    canonical_states = {
        row["canonical_state"]: dict(row)
        for row in db.execute("SELECT * FROM semantic_canonical_state WHERE state_domain='DEFECT' AND status='active'").fetchall()
    }
    scope = ""
    parameters: list[object] = []
    if subject_type:
        scope += " AND subject_type=?"
        parameters.append(subject_type)
    if subject_key:
        scope += " AND subject_key=?"
        parameters.append(subject_key)
    facts = db.execute(
        f"SELECT * FROM semantic_fact WHERE fact_type='canonical_defect_state' AND status IN ('derived','accepted'){scope}",
        parameters,
    ).fetchall()
    if not canonical_states:
        db.execute(
            "INSERT INTO semantic_state_transition_run(run_id,input_fact_count,accepted_transition_count,review_transition_count,current_state_count,status,source_write,formal_publication,created_at,scope_subject_type,scope_subject_key,subject_count) VALUES (?,?,?,?,?,'blocked',0,0,?,?,?,?)",
            (run_id, len(facts), 0, 0, 0, created, subject_type, subject_key, 0),
        )
        db.commit()
        db.close()
        return {"run_id": run_id, "input_fact_count": len(facts), "accepted_transition_count": 0, "review_transition_count": 0, "current_state_count": 0, "status": "blocked", "note": "没有可用的标准缺陷状态字典，状态迁移阻塞", "source_write": False, "formal_publication": False}

    rules = load_transition_rules(db)
    old_scope = ""
    old_parameters: list[object] = []
    if subject_type:
        old_scope += " AND subject_type=?"
        old_parameters.append(subject_type)
    if subject_key:
        old_scope += " AND subject_key=?"
        old_parameters.append(subject_key)
    old_states = {
        (row["subject_type"], row["subject_key"], row["state_domain"]): dict(row)
        for row in db.execute(f"SELECT * FROM semantic_current_state WHERE state_domain='DEFECT'{old_scope}", old_parameters).fetchall()
    }
    grouped: dict[tuple[str, str], list[dict[str, object]]] = defaultdict(list)
    for fact in facts:
        try:
            value = json.loads(fact["value_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            value = {}
        if not isinstance(value, dict):
            value = {}
        source_fact, source_event = source_event_for_fact(db, fact)
        event = semantic_event_for_source(db, source_fact)
        effective_at = (event["occurred_at"] if event is not None else None) or source_event.get("event_time") or (source_fact["observed_at"] if source_fact is not None else None) or fact["observed_at"]
        recorded_at = (event["recorded_at"] if event is not None else None) or (source_fact["created_at"] if source_fact is not None else None) or fact["created_at"]
        grouped[(fact["subject_type"], fact["subject_key"])].append({"fact": fact, "value": value, "source_fact": source_fact, "source_event": source_event, "event": event, "event_type": event_type_for(event, source_event), "effective_at": str(effective_at) if effective_at else None, "effective_dt": parse_time(effective_at), "recorded_at": str(recorded_at) if recorded_at else None, "recorded_dt": parse_time(recorded_at)})

    db.execute("BEGIN")
    if subject_type or subject_key:
        db.execute(f"DELETE FROM semantic_current_state WHERE state_domain='DEFECT'{old_scope}", old_parameters)
        db.execute(f"DELETE FROM semantic_state_transition WHERE state_domain='DEFECT'{old_scope}", old_parameters)
    else:
        db.execute("DELETE FROM semantic_current_state WHERE state_domain='DEFECT'")
        db.execute("DELETE FROM semantic_state_transition WHERE state_domain='DEFECT'")
    db.execute(
        "INSERT INTO semantic_state_transition_run(run_id,input_fact_count,accepted_transition_count,review_transition_count,current_state_count,status,source_write,formal_publication,created_at,scope_subject_type,scope_subject_key,subject_count,late_arrival_count,difference_count) VALUES (?,?,?,?,?,'needs_review',0,0,?,?,?,?,?,?)",
        (run_id, len(facts), 0, 0, 0, created, subject_type, subject_key, len(grouped), 0, 0),
    )

    accepted = 0
    needs_review = 0
    late_count = 0
    new_states: dict[tuple[str, str, str], dict[str, object]] = {}
    registered_event_types = {str(rule["event_type"]) for rule in rules}
    for subject, items in grouped.items():
        known = [item for item in items if item["effective_dt"] is not None]
        unknown = [item for item in items if item["effective_dt"] is None]
        late_items = {
            str(item["fact"]["fact_id"]) for item in known
            if any(other["recorded_dt"] and item["recorded_dt"] and other["effective_dt"] and other["effective_dt"] > item["effective_dt"] and other["recorded_dt"] < item["recorded_dt"] for other in known)
        }
        ordered = sorted(known, key=lambda item: (item["effective_dt"], item["recorded_dt"] or datetime.max.replace(tzinfo=timezone.utc), str(item["source_fact"]["source_schema"] if item["source_fact"] else ""), str(item["source_fact"]["source_table"] if item["source_fact"] else ""), str(item["source_fact"]["source_row_id"] if item["source_fact"] else ""), item["fact"]["fact_id"])) + sorted(unknown, key=lambda item: item["fact"]["fact_id"])
        current_state: str | None = None
        current_effective: str | None = None
        state_version = 0
        for item in ordered:
            fact = item["fact"]
            value = item["value"]
            to_state = str(value.get("canonical_state") or "").strip().upper()
            valid_target = to_state in canonical_states
            context = context_for(value, item["source_event"], item["event"])
            rule, guard_ok, guard_reason = choose_rule(rules, current_state, item["event_type"], to_state, context) if valid_target else (None, False, "canonical_state_unknown")
            has_time = item["effective_dt"] is not None
            late_arrival = str(fact["fact_id"]) in late_items
            late_count += int(late_arrival)
            if late_arrival:
                evidence_order = "effective_at_sorted_with_late_recorded_arrival"
            elif has_time:
                evidence_order = "effective_at_then_recorded_at_then_source_row"
            else:
                evidence_order = "missing_effective_at"
            # A canonical state fact may be the first approved snapshot for a
            # subject rather than a typed transition event.  Treat only that
            # first, time-backed, already-normalized observation as an initial
            # seed.  Later untyped status observations remain reviewable; this
            # preserves the transition gate and avoids inventing a path.
            snapshot_seed = (
                current_state is None
                and valid_target
                and has_time
                and rule is None
                and item["event_type"] not in registered_event_types
                and bool(value.get("dictionary_status_id") or value.get("mapping_version"))
            )
            if snapshot_seed:
                guard_ok = True
                guard_reason = "approved_snapshot_seed"
            transition_status = "accepted" if valid_target and has_time and ((rule is not None and guard_ok) or snapshot_seed) else "needs_review"
            if transition_status == "accepted":
                accepted += 1
            else:
                needs_review += 1
            transition_type = "initial" if current_state is None else ("replay" if current_state == to_state else "change")
            transition_id = sid("SST", "DEFECT", fact["subject_type"], fact["subject_key"], fact["fact_id"], RULE_VERSION_ID)
            evidence = {
                "trigger_fact_id": fact["fact_id"],
                "source_fact_id": value.get("derived_from_fact_id"),
                "source_event_id": item["source_event"].get("event_record_id"),
                "semantic_event_id": item["event"]["event_id"] if item["event"] is not None else None,
                "event_type": item["event_type"],
                "effective_at": item["effective_at"],
                "recorded_at": item["recorded_at"],
                "canonical_state": to_state,
                "rule_version": RULE_VERSION_ID,
                "transition_rule_id": rule["transition_rule_id"] if rule is not None else ("SBR:canonical-state-snapshot-seed" if snapshot_seed else None),
                "guard_reason": guard_reason,
                "order_evidence": evidence_order,
                "reason_code": "approved_snapshot_seed" if snapshot_seed else ("accepted" if transition_status == "accepted" else ("missing_effective_at" if not has_time else ("invalid_transition" if rule is None else "guard_failed"))),
            }
            db.execute(
                """
                INSERT INTO semantic_state_transition(
                  transition_id,subject_type,subject_key,state_domain,from_state,to_state,
                  transition_type,trigger_fact_id,event_id,rule_asset_id,rule_version_id,effective_at,
                  source_snapshot_id,evidence_json,confidence,status,created_at,transition_rule_id,
                  order_key,late_arrival,guard_status,replay_run_id
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (transition_id, fact["subject_type"], fact["subject_key"], "DEFECT", current_state, to_state or "UNKNOWN",
                 transition_type, fact["fact_id"], item["event"]["event_id"] if item["event"] is not None else None,
                 RULE_ASSET_ID, RULE_VERSION_ID, item["effective_at"], fact["source_snapshot_id"],
                 json.dumps(evidence, ensure_ascii=False), fact["confidence"], transition_status, created,
                 rule["transition_rule_id"] if rule is not None else ("SBR:canonical-state-snapshot-seed" if snapshot_seed else None), f"{item['effective_at'] or '9999-12-31'}|{item['recorded_at'] or ''}|{fact['fact_id']}", int(late_arrival), guard_reason, run_id),
            )
            if transition_status != "accepted":
                continue
            state = canonical_states[to_state]
            state_version += 1
            current_state = to_state
            current_effective = item["effective_at"]
            current_row = {
                "subject_type": fact["subject_type"], "subject_key": fact["subject_key"], "state_domain": "DEFECT",
                "current_state": to_state, "display_name": state["display_name"], "source_fact_id": fact["fact_id"],
                "transition_id": transition_id, "effective_at": current_effective, "source_snapshot_id": fact["source_snapshot_id"],
                "state_version": state_version, "status": "current", "updated_at": created,
            }
            new_states[(fact["subject_type"], fact["subject_key"], "DEFECT")] = current_row
            db.execute(
                """
                INSERT INTO semantic_current_state(
                  subject_type,subject_key,state_domain,current_state,display_name,
                  source_fact_id,transition_id,effective_at,source_snapshot_id,
                  state_version,status,updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(subject_type,subject_key,state_domain) DO UPDATE SET
                  current_state=excluded.current_state,
                  display_name=excluded.display_name,
                  source_fact_id=excluded.source_fact_id,
                  transition_id=excluded.transition_id,
                  effective_at=excluded.effective_at,
                  source_snapshot_id=excluded.source_snapshot_id,
                  state_version=excluded.state_version,
                  status=excluded.status,
                  updated_at=excluded.updated_at
                """,
                tuple(current_row[field] for field in ("subject_type","subject_key","state_domain","current_state","display_name","source_fact_id","transition_id","effective_at","source_snapshot_id","state_version","status","updated_at")),
            )

    all_keys = set(old_states) | set(new_states)
    differences: list[dict[str, object]] = []
    for key in sorted(all_keys):
        old = old_states.get(key)
        new = new_states.get(key)
        if old is None and new is not None:
            difference_type = "added"
        elif old is not None and new is None:
            difference_type = "removed"
        elif old and new and old["current_state"] != new["current_state"]:
            difference_type = "state_changed"
        elif old and new and old.get("effective_at") != new.get("effective_at"):
            difference_type = "effective_time_changed"
        else:
            continue
        difference = {
            "subject_type": key[0], "subject_key": key[1], "state_domain": key[2], "difference_type": difference_type,
            "previous_state": old.get("current_state") if old else None, "replay_state": new.get("current_state") if new else None,
            "previous_effective_at": old.get("effective_at") if old else None, "replay_effective_at": new.get("effective_at") if new else None,
            "evidence": {"replay_version": RULE_VERSION_ID, "scope_subject_type": subject_type, "scope_subject_key": subject_key},
        }
        differences.append(difference)
        db.execute(
            "INSERT INTO semantic_state_replay_diff(diff_id,replay_run_id,subject_type,subject_key,state_domain,difference_type,previous_state,replay_state,previous_effective_at,replay_effective_at,evidence_json,status,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sid("SSRD", run_id, *key, difference_type), run_id, *key, difference_type, difference["previous_state"], difference["replay_state"], difference["previous_effective_at"], difference["replay_effective_at"], json.dumps(difference["evidence"], ensure_ascii=False), "reported", created),
        )
    current_count = int(db.execute("SELECT count(*) FROM semantic_current_state WHERE state_domain='DEFECT'").fetchone()[0])
    status = "needs_review" if needs_review else "completed"
    note = "按有效时间完成主体级状态重放" if not needs_review else "部分事件缺少有效时间、迁移规则或门禁证据，已隔离待复核"
    db.execute(
        "UPDATE semantic_state_transition_run SET accepted_transition_count=?,review_transition_count=?,current_state_count=?,status=?,replay_version=?,subject_count=?,late_arrival_count=?,difference_count=? WHERE run_id=?",
        (accepted, needs_review, current_count, status, RULE_VERSION_ID, len(grouped), late_count, len(differences), run_id),
    )
    db.commit()
    db.close()
    return {"run_id": run_id, "input_fact_count": len(facts), "accepted_transition_count": accepted, "review_transition_count": needs_review, "current_state_count": current_count, "status": status, "note": note, "replayVersion": RULE_VERSION_ID, "subjectCount": len(grouped), "lateArrivalCount": late_count, "differenceCount": len(differences), "differences": differences[:100], "source_write": False, "formal_publication": False}


def execute(target_path: pathlib.Path, subject_type: str | None = None, subject_key: str | None = None) -> dict[str, object]:
    return replay(target_path, subject_type=subject_type, subject_key=subject_key)


def main() -> None:
    parser = argparse.ArgumentParser(description="Execute canonical defect state transitions locally")
    parser.add_argument("--target-db", type=pathlib.Path, default=DEFAULT_TARGET)
    parser.add_argument("--subject-type")
    parser.add_argument("--subject-key")
    args = parser.parse_args()
    print(json.dumps(execute(args.target_db.resolve(), args.subject_type, args.subject_key), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
