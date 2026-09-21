"""Deterministic knowledge conflict detection and bounded replay."""
from __future__ import annotations

import hashlib
import json
import pathlib
import sqlite3
from datetime import datetime, timezone
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _definition(connection: sqlite3.Connection, asset_id: str, version: str | None = None) -> dict[str, Any]:
    row = connection.execute("SELECT definition_json FROM knowledge_asset_version WHERE asset_id=? AND version=COALESCE(?,version) ORDER BY created_at DESC LIMIT 1", (asset_id, version)).fetchone()
    if row is None:
        return {}
    try:
        value = json.loads(row[0])
        return value if isinstance(value, dict) else {"value": value}
    except json.JSONDecodeError:
        return {"content": row[0]}


def _conflict_key(definition: dict[str, Any]) -> str | None:
    subject = definition.get("appliesTo") or definition.get("appliesToClass") or definition.get("subjectKey")
    predicate = definition.get("predicate") or definition.get("property") or definition.get("metric")
    unit = definition.get("unit") or ""
    if not subject or not predicate:
        return None
    return "|".join(str(value).strip().lower() for value in (subject, predicate, unit))


def detect_conflicts(connection: sqlite3.Connection) -> dict[str, Any]:
    """Find different values for the same semantic subject/property/unit."""
    rows = connection.execute("SELECT asset_id,asset_key,status,current_version FROM knowledge_asset WHERE status IN ('proposed','approved','published','enabled') ORDER BY asset_id").fetchall()
    groups: dict[str, list[tuple[sqlite3.Row, dict[str, Any]]]] = {}
    for row in rows:
        definition = _definition(connection, row["asset_id"], row["current_version"])
        key = _conflict_key(definition)
        if key:
            groups.setdefault(key, []).append((row, definition))
    conflicts: list[dict[str, Any]] = []
    now = _now()
    for key, items in groups.items():
        values = {json.dumps(item.get("value", item.get("limit", item.get("canonical", ""))), ensure_ascii=False, sort_keys=True) for _, item in items}
        if len(items) < 2 or len(values) < 2:
            continue
        asset_ids = [row["asset_id"] for row, _ in items]
        conflict_id = "KCON-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]
        details = {"conflictKey": key, "assetIds": asset_ids, "values": sorted(values), "policy": "keep_all_isolate_until_review"}
        for asset_id in asset_ids:
            connection.execute(
                "INSERT OR IGNORE INTO knowledge_asset_issue(issue_id,asset_id,issue_type,severity,status,details_json,conflict_key,created_at) VALUES (?,?,?,?,?,?,?,?)",
                (conflict_id + "-" + asset_id[-8:], asset_id, "conflict", "medium", "open", json.dumps(details, ensure_ascii=False), key, now),
            )
        conflicts.append({"conflictId": conflict_id, "conflictKey": key, "assetIds": asset_ids, "values": sorted(values)})
    connection.commit()
    return {"status": "completed", "conflictCount": len(conflicts), "conflicts": conflicts, "sourceWrite": False, "formalPublication": False}


def _evaluate(definition: dict[str, Any], facts: dict[str, Any]) -> Any:
    condition = definition.get("condition")
    if not isinstance(condition, dict):
        return None
    field = str(condition.get("field") or "")
    left = facts.get(field)
    right = condition.get("value")
    if left is None:
        return "unknown"
    try:
        left_number, right_number = float(left), float(right)
    except (TypeError, ValueError):
        left_number, right_number = str(left), str(right)
    operator = str(condition.get("operator") or "eq").lower()
    result = {"eq": left_number == right_number, "=": left_number == right_number, "gt": left_number > right_number, ">": left_number > right_number, "gte": left_number >= right_number, ">=": left_number >= right_number, "lt": left_number < right_number, "<": left_number < right_number, "lte": left_number <= right_number, "<=": left_number <= right_number}.get(operator)
    return result if result is not None else "unknown"


def replay_knowledge(connection: sqlite3.Connection, asset_id: str, cases: list[dict[str, Any]]) -> dict[str, Any]:
    """Replay the current knowledge condition against explicit local cases."""
    asset = connection.execute("SELECT asset_id,current_version FROM knowledge_asset WHERE asset_id=?", (asset_id,)).fetchone()
    if asset is None:
        raise LookupError("知识资产不存在")
    definition = _definition(connection, asset_id)
    results = [{"caseId": str(case.get("caseId") or index), "result": _evaluate(definition, case.get("facts", {})), "facts": case.get("facts", {})} for index, case in enumerate(cases, start=1)]
    replay_id = "KREP-" + hashlib.sha256((asset_id + json.dumps(cases, ensure_ascii=False, sort_keys=True)).encode("utf-8")).hexdigest()[:24]
    payload = {"schemaVersion": "knowledge-replay-v1", "replayId": replay_id, "assetId": asset_id, "version": asset["current_version"], "results": results, "passCount": sum(result["result"] is not None and result["result"] != "unknown" for result in results), "unknownCount": sum(result["result"] == "unknown" for result in results), "sourceWrite": False, "formalPublication": False}
    output = pathlib.Path(__file__).resolve().parents[3] / "reports" / "knowledge-replays"
    output.mkdir(parents=True, exist_ok=True)
    path = output / f"{replay_id}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return {**payload, "reportPath": str(path)}
