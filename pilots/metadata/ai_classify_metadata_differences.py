"""AI classification for GRP metadata quality candidates.

This module is deliberately recommendation-only. It reads the local GRP
catalog, asks the configured OpenAI-compatible model to classify quality
findings, and writes an auditable judgment package plus a versioned metadata
semantic dictionary. It never writes to DM8 or to the formal publication
layer.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import pathlib
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any

import httpx
from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAI


CATEGORIES = (
    "true_semantic_difference",
    "system_configuration_difference",
    "data_quality_or_missing_link",
    "needs_review",
)
JUDGMENT_FIELDS = [
    "record_id",
    "source_schema",
    "finding_type",
    "semantic_key",
    "pattern_id",
    "category",
    "confidence",
    "reason",
    "evidence",
    "judgment_source",
    "judge_version",
    "judgment_status",
]
JUDGE_VERSION = "grp-metadata-ai-classifier-20260815-v1"


def clean(value: object) -> str:
    return "" if value is None else str(value).strip()


def to_int(value: object, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def to_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def sha(value: object) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:20]


def read_csv(path: pathlib.Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return [{str(k): clean(v) for k, v in row.items()} for row in csv.DictReader(handle)]


def write_csv(path: pathlib.Path, fields: list[str], rows: list[dict[str, object]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows({field: row.get(field, "") for field in fields} for row in rows)


def catalog_rows(catalog_dir: pathlib.Path, filename: str) -> list[dict[str, str]]:
    path = catalog_dir / filename
    if not path.exists():
        raise SystemExit(f"Missing catalog file: {path}")
    return read_csv(path)


def index_rows(rows: list[dict[str, str]], *fields: str) -> dict[tuple[str, ...], list[dict[str, str]]]:
    result: dict[tuple[str, ...], list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        result[tuple(clean(row.get(field)) for field in fields)].append(row)
    return result


def compact_evidence(row: dict[str, str]) -> dict[str, str]:
    fields = (
        "object_name", "entity_name", "description", "class_name", "binding_status",
        "attribute_name", "column_name", "semantic_label_candidate", "data_type",
        "length", "scale", "required", "domain_id", "primary_key_sequence",
        "same_as_object", "same_as_attribute", "relationship_name", "parent_object",
        "child_object", "cardinality", "db_join_required", "where_clause",
        "table_name", "unique_column_name", "view_name", "view_column_name",
        "index_name", "unique_rule", "column_sequence", "ordering",
    )
    return {field: clean(row.get(field))[:500] for field in fields if clean(row.get(field))}


def enrich_finding(
    finding: dict[str, str],
    object_by_key: dict[tuple[str, str], list[dict[str, str]]],
    object_by_metadata_key: dict[tuple[str, str], list[dict[str, str]]],
    attribute_by_metadata_key: dict[tuple[str, str], list[dict[str, str]]],
    relationship_by_metadata_key: dict[tuple[str, str], list[dict[str, str]]],
) -> dict[str, Any]:
    schema = clean(finding.get("source_schema"))
    semantic_key = clean(finding.get("semantic_key"))
    finding_type = clean(finding.get("finding_type"))
    candidates: list[dict[str, str]] = []
    if finding_type == "object_table_unmatched":
        candidates = object_by_key.get((schema, semantic_key), [])
    elif finding_type in {"duplicate_object_key"}:
        candidates = object_by_metadata_key.get((schema, semantic_key), [])
    elif finding_type.startswith("attribute") or finding_type == "duplicate_attribute_key":
        candidates = attribute_by_metadata_key.get((schema, semantic_key), [])
    elif finding_type.startswith("relationship") or finding_type == "duplicate_relationship_key":
        candidates = relationship_by_metadata_key.get((schema, semantic_key), [])
    representative = candidates[0] if candidates else {}
    return {
        "recordId": f"metadata-quality-{sha(f'{schema}|{finding_type}|{semantic_key}')} ".strip(),
        "sourceSchema": schema,
        "findingType": finding_type,
        "semanticKey": semantic_key,
        "severity": clean(finding.get("severity")),
        "count": to_int(clean(finding.get("count")), 1),
        "message": clean(finding.get("message"))[:500],
        "evidence": compact_evidence(representative),
    }


def build_records(catalog_dir: pathlib.Path) -> list[dict[str, Any]]:
    findings = catalog_rows(catalog_dir, "metadata_quality_findings.csv")
    objects = catalog_rows(catalog_dir, "metadata_objects.csv")
    attributes = catalog_rows(catalog_dir, "metadata_attributes.csv")
    relationships = catalog_rows(catalog_dir, "metadata_relationships.csv")
    object_by_key = index_rows(objects, "source_schema", "semantic_object_key")
    object_by_metadata_key = index_rows(objects, "source_schema", "metadata_row_key")
    attribute_by_metadata_key = index_rows(attributes, "source_schema", "metadata_row_key")
    relationship_by_metadata_key = index_rows(relationships, "source_schema", "metadata_row_key")
    records = [
        enrich_finding(
            finding,
            object_by_key,
            object_by_metadata_key,
            attribute_by_metadata_key,
            relationship_by_metadata_key,
        )
        for finding in findings
    ]
    records.sort(key=lambda item: (item["findingType"], item["sourceSchema"], item["semanticKey"]))
    return records


def parse_json_response(text: str) -> Any:
    value = (text or "").strip()
    if value.startswith("```"):
        value = value.split("\n", 1)[1] if "\n" in value else value
        if value.endswith("```"):
            value = value[:-3].strip()
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        start, end = value.find("{"), value.rfind("}")
        if start < 0 or end <= start:
            raise
        return json.loads(value[start : end + 1])


def classify_batch(
    client: OpenAI,
    model: str,
    batch: list[dict[str, Any]],
    max_tokens: int,
    timeout_label: str,
) -> list[dict[str, Any]]:
    payload = {
        "task": "分类 GRP 元数据质量候选问题的差异性质，不修改源数据",
        "outputSchema": {
            "items": [
                {
                    "recordId": "must equal an input recordId",
                    "category": "true_semantic_difference|system_configuration_difference|data_quality_or_missing_link|needs_review",
                    "confidence": 0.0,
                    "reason": "short evidence-based reason",
                    "evidence": ["input field or observation used"],
                }
            ]
        },
        "policy": [
            "只能使用输入记录及其 evidence，不得补造数据库事实。",
            "true_semantic_difference：对象、字段、关系的业务含义或定义确实冲突。",
            "system_configuration_difference：租户/组织范围、长度、索引属性、WHERECLAUSE、技术绑定等部署配置差异。",
            "data_quality_or_missing_link：父对象、表绑定、关系端点缺失或重复元数据，优先作为数据质量问题。",
            "证据不足、多个解释都成立或可能影响业务含义时，必须返回 needs_review。",
            "不要提出改写、删除、发布或写回操作。只返回 JSON。",
        ],
        "records": batch,
    }
    try:
        completion = client.chat.completions.create(
            model=model,
            max_tokens=max_tokens,
            temperature=0,
            extra_body={"thinking": {"type": os.getenv("RULE_AGENT_THINKING", "disabled").strip().lower() or "disabled"}},
            messages=[
                {"role": "system", "content": "你是保守的数据库元数据语义审查智能体。只输出合法 JSON，不输出 Markdown。"},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
        )
    except (APIStatusError, APIConnectionError, APITimeoutError, TimeoutError) as exc:
        raise RuntimeError(f"AI request failed ({timeout_label}): {type(exc).__name__}") from exc
    message = completion.choices[0].message if completion.choices else None
    text = message.content if message else ""
    if not text:
        finish = completion.choices[0].finish_reason if completion.choices else "no_choice"
        reasoning = getattr(message, "reasoning_content", "") if message else ""
        raise RuntimeError(f"AI response is empty ({timeout_label}): finish={finish}, reasoning_length={len(reasoning or '')}")
    try:
        parsed = parse_json_response(text)
    except json.JSONDecodeError as exc:
        excerpt = clean(text)[:1200]
        raise RuntimeError(f"AI returned invalid JSON ({timeout_label}): {excerpt}") from exc
    items = parsed.get("items", []) if isinstance(parsed, dict) else parsed
    if not isinstance(items, list):
        raise RuntimeError(f"AI response has no items array ({timeout_label})")
    return [item for item in items if isinstance(item, dict)]


def canonicalize_batch(raw_items: list[dict[str, Any]], batch: list[dict[str, Any]]) -> list[dict[str, Any]]:
    allowed = {str(item["recordId"]): item for item in batch}
    output: dict[str, dict[str, Any]] = {}
    for item in raw_items:
        record_id = clean(item.get("recordId") or item.get("record_id"))
        if record_id not in allowed or record_id in output:
            continue
        category = clean(item.get("category")).lower()
        if category not in CATEGORIES:
            category = "needs_review"
        try:
            confidence = max(0.0, min(1.0, float(item.get("confidence") or 0)))
        except (TypeError, ValueError):
            confidence = 0.0
        output[record_id] = {
            "record_id": record_id,
            "source_schema": allowed[record_id]["sourceSchema"],
            "finding_type": allowed[record_id]["findingType"],
            "semantic_key": allowed[record_id]["semanticKey"],
            "pattern_id": "",
            "category": category,
            "confidence": f"{confidence:.4f}",
            "reason": clean(item.get("reason"))[:1500] or "AI 未提供理由",
            "evidence": json.dumps(item.get("evidence") if isinstance(item.get("evidence"), list) else [], ensure_ascii=False),
            "judgment_source": "ai_record",
            "judge_version": JUDGE_VERSION,
            "judgment_status": "recommendation_only",
        }
    for item in batch:
        output.setdefault(
            item["recordId"],
            {
                "record_id": item["recordId"],
                "source_schema": item["sourceSchema"],
                "finding_type": item["findingType"],
                "semantic_key": item["semanticKey"],
                "pattern_id": "",
                "category": "needs_review",
                "confidence": "0.0000",
                "reason": "AI 未返回该记录，自动降级为需要复核",
                "evidence": "[]",
                "judgment_source": "ai_record_fallback",
                "judge_version": JUDGE_VERSION,
                "judgment_status": "recommendation_only",
            },
        )
    return list(output.values())


def build_pattern_inputs(records: list[dict[str, Any]], catalog_dir: pathlib.Path) -> list[dict[str, Any]]:
    patterns: list[dict[str, Any]] = []
    by_type: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        by_type[record["findingType"]].append(record)
    for finding_type, items in sorted(by_type.items()):
        patterns.append(
            {
                "patternId": f"quality:{finding_type}",
                "kind": "quality_finding",
                "findingType": finding_type,
                "count": len(items),
                "representativeRecords": items[:8],
            }
        )
    diff_files = (
        ("object", "object_cross_schema_diff.csv"),
        ("attribute", "attribute_cross_schema_diff.csv"),
        ("relationship", "relationship_cross_schema_diff.csv"),
        ("table", "table_cross_schema_diff.csv"),
        ("view", "view_cross_schema_diff.csv"),
        ("view_column", "view_column_cross_schema_diff.csv"),
        ("index", "index_cross_schema_diff.csv"),
        ("index_column", "index_column_cross_schema_diff.csv"),
    )
    by_diff: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for artifact_type, filename in diff_files:
        for row in read_csv(catalog_dir / filename):
            if row.get("status") == "needs_review":
                by_diff[(artifact_type, row.get("differences", ""))].append(row)
    for (artifact_type, difference_signature), items in sorted(by_diff.items()):
        patterns.append(
            {
                "patternId": f"cross:{artifact_type}:{sha(difference_signature)}",
                "kind": "cross_schema_difference",
                "artifactType": artifact_type,
                "differenceSignature": difference_signature,
                "count": len(items),
                "representativeRecords": [
                    {
                        "semanticKey": row.get("semantic_key", ""),
                        "status": row.get("status", ""),
                        "hdCount": row.get("hd_count", ""),
                        "xnyCount": row.get("xny_count", ""),
                        "differences": row.get("differences", ""),
                    }
                    for row in items[:8]
                ],
            }
        )
    return patterns


def classify_patterns(
    client: OpenAI,
    model: str,
    patterns: list[dict[str, Any]],
    max_tokens: int,
) -> list[dict[str, Any]]:
    payload = {
        "task": "按异常模式判断 GRP 元数据差异性质，并将判断映射到同类记录",
        "outputSchema": {
            "patterns": [
                {
                    "patternId": "must equal an input patternId",
                    "category": "true_semantic_difference|system_configuration_difference|data_quality_or_missing_link|needs_review",
                    "confidence": 0.0,
                    "reason": "short evidence-based reason",
                    "evidence": ["input field or observation used"],
                }
            ]
        },
        "policy": [
            "只能使用输入模式及代表性记录，不得补造数据库事实。",
            "真正业务含义、字段定义、对象分类或关系语义冲突，归 true_semantic_difference。",
            "租户/组织范围、字段长度、索引属性、技术绑定、WHERECLAUSE 等部署差异，归 system_configuration_difference。",
            "父对象、表绑定、关系端点缺失或重复元数据，归 data_quality_or_missing_link。",
            "证据不足、模式内部混杂或可能影响业务含义时，归 needs_review。",
            "不要提出改写、删除、发布或写回操作。只返回 JSON。",
        ],
        "patterns": patterns,
    }
    try:
        completion = client.chat.completions.create(
            model=model,
            max_tokens=max_tokens,
            temperature=0,
            extra_body={"thinking": {"type": os.getenv("RULE_AGENT_THINKING", "disabled").strip().lower() or "disabled"}},
            messages=[
                {"role": "system", "content": "你是保守的数据库元数据语义审查智能体。只输出合法 JSON，不输出 Markdown。"},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
        )
    except (APIStatusError, APIConnectionError, APITimeoutError, TimeoutError) as exc:
        raise RuntimeError(f"AI pattern request failed: {type(exc).__name__}") from exc
    message = completion.choices[0].message if completion.choices else None
    text = message.content if message else ""
    if not text:
        finish = completion.choices[0].finish_reason if completion.choices else "no_choice"
        reasoning = getattr(message, "reasoning_content", "") if message else ""
        raise RuntimeError(f"AI pattern response is empty: finish={finish}, reasoning_length={len(reasoning or '')}")
    try:
        parsed = parse_json_response(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"AI pattern response is invalid JSON: {clean(text)[:1200]}") from exc
    items = parsed.get("patterns", []) if isinstance(parsed, dict) else parsed
    if not isinstance(items, list):
        raise RuntimeError("AI pattern response has no patterns array")
    return [item for item in items if isinstance(item, dict)]


def canonicalize_patterns(raw_items: list[dict[str, Any]], patterns: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    allowed = {str(item["patternId"]): item for item in patterns}
    output: dict[str, dict[str, Any]] = {}
    for item in raw_items:
        pattern_id = clean(item.get("patternId") or item.get("pattern_id"))
        if pattern_id not in allowed or pattern_id in output:
            continue
        category = clean(item.get("category")).lower()
        if category not in CATEGORIES:
            category = "needs_review"
        try:
            confidence = max(0.0, min(1.0, float(item.get("confidence") or 0)))
        except (TypeError, ValueError):
            confidence = 0.0
        output[pattern_id] = {
            "pattern_id": pattern_id,
            "category": category,
            "confidence": f"{confidence:.4f}",
            "reason": clean(item.get("reason"))[:1500] or "AI 未提供理由",
            "evidence": json.dumps(item.get("evidence") if isinstance(item.get("evidence"), list) else [], ensure_ascii=False),
            "judgment_source": "ai_pattern",
        }
    for pattern in patterns:
        output.setdefault(
            pattern["patternId"],
            {
                "pattern_id": pattern["patternId"],
                "category": "needs_review",
                "confidence": "0.0000",
                "reason": "AI 未返回该模式，自动降级为需要复核",
                "evidence": "[]",
                "judgment_source": "ai_pattern_fallback",
            },
        )
    return output


def apply_pattern_judgments(records: list[dict[str, Any]], pattern_results: dict[str, dict[str, Any]]) -> list[dict[str, str]]:
    output: list[dict[str, str]] = []
    for record in records:
        result = pattern_results[f"quality:{record['findingType']}"]
        output.append(
            {
                "record_id": record["recordId"],
                "source_schema": record["sourceSchema"],
                "finding_type": record["findingType"],
                "semantic_key": record["semanticKey"],
                "pattern_id": result["pattern_id"],
                "category": result["category"],
                "confidence": result["confidence"],
                "reason": result["reason"],
                "evidence": result["evidence"],
                "judgment_source": result["judgment_source"],
                "judge_version": JUDGE_VERSION,
                "judgment_status": "recommendation_only",
            }
        )
    return output


def write_cross_schema_judgments(
    catalog_dir: pathlib.Path,
    output_dir: pathlib.Path,
    pattern_results: dict[str, dict[str, Any]],
) -> pathlib.Path:
    diff_files = (
        ("object", "object_cross_schema_diff.csv"),
        ("attribute", "attribute_cross_schema_diff.csv"),
        ("relationship", "relationship_cross_schema_diff.csv"),
        ("table", "table_cross_schema_diff.csv"),
        ("view", "view_cross_schema_diff.csv"),
        ("view_column", "view_column_cross_schema_diff.csv"),
        ("index", "index_cross_schema_diff.csv"),
        ("index_column", "index_column_cross_schema_diff.csv"),
    )
    rows: list[dict[str, str]] = []
    for artifact_type, filename in diff_files:
        for row in read_csv(catalog_dir / filename):
            if row.get("status") != "needs_review":
                continue
            pattern_id = f"cross:{artifact_type}:{sha(row.get('differences', ''))}"
            result = pattern_results[pattern_id]
            rows.append(
                {
                    "record_id": f"cross-schema-{sha(artifact_type + '|' + row.get('semantic_key', ''))}",
                    "artifact_type": artifact_type,
                    "semantic_key": row.get("semantic_key", ""),
                    "status": row.get("status", ""),
                    "differences": row.get("differences", ""),
                    "hd_count": row.get("hd_count", ""),
                    "xny_count": row.get("xny_count", ""),
                    "pattern_id": pattern_id,
                    "category": result["category"],
                    "confidence": result["confidence"],
                    "reason": result["reason"],
                    "evidence": result["evidence"],
                    "judgment_source": result["judgment_source"],
                    "judge_version": JUDGE_VERSION,
                    "judgment_status": "recommendation_only",
                }
            )
    path = output_dir / "ai_cross_schema_difference_judgments.csv"
    write_csv(path, list(rows[0]) if rows else [], rows)
    return path


def load_existing(path: pathlib.Path) -> dict[str, dict[str, str]]:
    if not path.exists():
        return {}
    return {row["record_id"]: row for row in read_csv(path) if row.get("record_id")}


def aggregate(values: list[str]) -> str:
    unique = sorted({clean(value) for value in values if clean(value)})
    return " / ".join(unique[:5])


def diff_statuses(catalog_dir: pathlib.Path, filename: str) -> dict[str, str]:
    return {row["semantic_key"]: row["status"] for row in read_csv(catalog_dir / filename)}


def build_dictionary(catalog_dir: pathlib.Path, output_dir: pathlib.Path, judgments: list[dict[str, str]]) -> pathlib.Path:
    config = [
        ("object", "metadata_objects.csv", "object_cross_schema_diff.csv", "semantic_object_key", "object_name"),
        ("attribute", "metadata_attributes.csv", "attribute_cross_schema_diff.csv", "cross_schema_key", "attribute_name"),
        ("table", "metadata_tables.csv", "table_cross_schema_diff.csv", "cross_schema_key", "table_name"),
        ("relationship", "metadata_relationships.csv", "relationship_cross_schema_diff.csv", "cross_schema_key", "relationship_name"),
        ("view", "metadata_views.csv", "view_cross_schema_diff.csv", "cross_schema_key", "view_name"),
        ("view_column", "metadata_view_columns.csv", "view_column_cross_schema_diff.csv", "cross_schema_key", "view_column_name"),
        ("index", "metadata_indexes.csv", "index_cross_schema_diff.csv", "cross_schema_key", "index_name"),
        ("index_column", "metadata_index_columns.csv", "index_column_cross_schema_diff.csv", "cross_schema_key", "column_name"),
    ]
    judgment_by_source_key: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for judgment in judgments:
        judgment_by_source_key[(judgment["source_schema"], judgment["semantic_key"])].append(judgment)
    rows: list[dict[str, object]] = []
    for concept_type, source_file, diff_file, key_field, name_field in config:
        source_rows = read_csv(catalog_dir / source_file)
        status_map = diff_statuses(catalog_dir, diff_file)
        groups: dict[str, list[dict[str, str]]] = defaultdict(list)
        for row in source_rows:
            groups[clean(row.get("cross_schema_key") or row.get(key_field))].append(row)
        for semantic_key, group in sorted(groups.items()):
            if not semantic_key:
                continue
            statuses = [status_map.get(semantic_key, "not_compared")]
            evidence_rows: list[dict[str, str]] = []
            for source_row in group:
                evidence_rows.extend(
                    judgment_by_source_key.get((source_row.get("source_schema", ""), source_row.get("metadata_row_key", "")), [])
                )
                evidence_rows.extend(
                    judgment_by_source_key.get((source_row.get("source_schema", ""), source_row.get("semantic_object_key", "")), [])
                )
            category = aggregate([row["category"] for row in evidence_rows])
            confidence = aggregate([row["confidence"] for row in evidence_rows])
            reasons = aggregate([row["reason"] for row in evidence_rows])
            first = group[0]
            labels = aggregate([row.get("semantic_label_candidate", "") for row in group])
            descriptions = aggregate([row.get("description", "") for row in group])
            names = aggregate([row.get(name_field, "") for row in group])
            table_or_parent = aggregate([
                row.get("table_name", "") or row.get("parent_object", "") or row.get("object_name", "")
                for row in group
            ])
            rows.append(
                {
                    "dictionary_version": "pending",
                    "concept_type": concept_type,
                    "semantic_key": semantic_key,
                    "canonical_name": names,
                    "semantic_label_candidate": labels,
                    "description": descriptions,
                    "data_type": aggregate([row.get("data_type", "") for row in group]),
                    "length": aggregate([row.get("length", "") for row in group]),
                    "required": aggregate([row.get("required", "") for row in group]),
                    "domain_id": aggregate([row.get("domain_id", "") for row in group]),
                    "parent_or_table": table_or_parent,
                    "source_schemas": aggregate([row.get("source_schema", "") for row in group]),
                    "cross_schema_status": statuses[0],
                    "ai_category": category,
                    "ai_confidence": confidence,
                    "ai_reason": reasons,
                    "semantic_status": "aligned" if statuses[0] == "same" else "review_required" if statuses[0] == "needs_review" else "source_specific",
                    "evidence": aggregate([row.get("evidence", "") for row in group]),
                }
            )
    judgment_id = output_dir.name
    version = f"metadata-semantic-dictionary-{judgment_id}"
    for row in rows:
        row["dictionary_version"] = version
    path = output_dir / "metadata_semantic_dictionary.csv"
    write_csv(path, list(rows[0]) if rows else [], rows)
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description="AI classify GRP metadata quality candidates")
    parser.add_argument("--catalog-dir", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--mode", choices=("pattern", "record"), default="pattern")
    parser.add_argument("--batch-size", type=int, default=50)
    parser.add_argument("--pattern-batch-size", type=int, default=10)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--max-tokens", type=int, default=to_int(os.getenv("RULE_AGENT_MAX_TOKENS"), 4096))
    parser.add_argument("--retry-count", type=int, default=2)
    args = parser.parse_args()
    catalog_dir = pathlib.Path(args.catalog_dir).resolve()
    output_dir = pathlib.Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    output_csv = output_dir / "ai_metadata_difference_judgments.csv"
    records = build_records(catalog_dir)
    if args.limit > 0:
        records = records[: args.limit]
    if args.batch_size < 1 or args.batch_size > 100:
        raise SystemExit("--batch-size must be between 1 and 100")
    if args.pattern_batch_size < 1 or args.pattern_batch_size > 20:
        raise SystemExit("--pattern-batch-size must be between 1 and 20")
    base_url = clean(os.getenv("RULE_AGENT_BASE_URL")) or clean(os.getenv("ANTHROPIC_BASE_URL"))
    api_key = clean(os.getenv("RULE_AGENT_API_KEY")) or clean(os.getenv("ANTHROPIC_AUTH_TOKEN"))
    model = clean(os.getenv("RULE_AGENT_MODEL")) or clean(os.getenv("ANTHROPIC_MODEL")) or "deepseek-v4-pro"
    if not base_url or not api_key:
        raise SystemExit("AI configuration is missing: RULE_AGENT_BASE_URL/RULE_AGENT_API_KEY")
    if not base_url.rstrip("/").endswith("/v1"):
        base_url = base_url.rstrip("/") + "/v1"
    # The local Windows environment may expose an unavailable HTTP proxy.
    # The VPN route is already available directly, so bypass inherited proxy
    # variables for this controlled outbound call.
    http_client = httpx.Client(trust_env=False, timeout=to_float(os.getenv("RULE_AGENT_TIMEOUT"), 180.0))
    client = OpenAI(base_url=base_url, api_key=api_key, timeout=to_float(os.getenv("RULE_AGENT_TIMEOUT"), 180.0), max_retries=0, http_client=http_client)
    pattern_count = 0
    cross_judgment_path: pathlib.Path | None = None
    if args.mode == "pattern":
        patterns = build_pattern_inputs(records, catalog_dir)
        pattern_results: dict[str, dict[str, Any]] = {}
        pattern_batches = [patterns[index : index + args.pattern_batch_size] for index in range(0, len(patterns), args.pattern_batch_size)]
        for batch_index, pattern_batch in enumerate(pattern_batches, start=1):
            raw_patterns: list[dict[str, Any]] = []
            last_error: Exception | None = None
            for attempt in range(args.retry_count + 1):
                try:
                    raw_patterns = classify_patterns(client, model, pattern_batch, args.max_tokens)
                    last_error = None
                    break
                except Exception as exc:
                    last_error = exc
                    if attempt < args.retry_count:
                        time.sleep(2 ** attempt)
            if last_error is not None:
                raise SystemExit(str(last_error))
            pattern_results.update(canonicalize_patterns(raw_patterns, pattern_batch))
            print(json.dumps({"patternBatch": batch_index, "patternBatches": len(pattern_batches), "patternsJudged": len(pattern_results), "patternsTotal": len(patterns)}, ensure_ascii=False), flush=True)
        (output_dir / "pattern_judgments.json").write_text(json.dumps({"patterns": patterns, "judgments": list(pattern_results.values())}, ensure_ascii=False, indent=2), encoding="utf-8")
        judgments = apply_pattern_judgments(records, pattern_results)
        write_csv(output_csv, JUDGMENT_FIELDS, judgments)
        cross_judgment_path = write_cross_schema_judgments(catalog_dir, output_dir, pattern_results)
        pattern_count = len(patterns)
    else:
        existing = load_existing(output_csv)
        pending = [record for record in records if record["recordId"] not in existing]
        total_batches = (len(pending) + args.batch_size - 1) // args.batch_size
        for index in range(0, len(pending), args.batch_size):
            batch = pending[index : index + args.batch_size]
            raw: list[dict[str, Any]] = []
            last_error: Exception | None = None
            for attempt in range(args.retry_count + 1):
                try:
                    raw = classify_batch(client, model, batch, args.max_tokens, f"batch {index // args.batch_size + 1}/{total_batches}")
                    last_error = None
                    break
                except Exception as exc:  # keep partial output resumable
                    last_error = exc
                    if attempt < args.retry_count:
                        time.sleep(2 ** attempt)
            if last_error is not None:
                write_csv(output_csv, JUDGMENT_FIELDS, list(existing.values()))
                raise SystemExit(str(last_error))
            existing.update({row["record_id"]: row for row in canonicalize_batch(raw, batch)})
            write_csv(output_csv, JUDGMENT_FIELDS, sorted(existing.values(), key=lambda row: row["record_id"]))
            print(json.dumps({"batch": index // args.batch_size + 1, "batches": total_batches, "judged": len(existing), "total": len(records)}, ensure_ascii=False), flush=True)
        judgments = list(existing.values())

    counts = Counter(row["category"] for row in judgments)
    dictionary_path = build_dictionary(catalog_dir, output_dir, judgments)
    catalog_manifest = json.loads((catalog_dir / "manifest.json").read_text(encoding="utf-8"))
    manifest = {
        "judgment_id": output_dir.name,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "judge_version": JUDGE_VERSION,
        "mode": args.mode,
        "model": model,
        "input_catalog": str(catalog_dir),
        "catalog_run_id": catalog_manifest.get("run_id"),
        "record_count": len(records),
        "judged_count": len(judgments),
        "pattern_count": pattern_count,
        "category_counts": dict(sorted(counts.items())),
        "judgment_file": str(output_csv),
        "cross_schema_judgment_file": str(cross_judgment_path) if cross_judgment_path else "",
        "dictionary_file": str(dictionary_path),
        "judgment_status": "recommendation_only",
        "source_write": False,
        "formal_publication": False,
        "policy": "AI classification is advisory; only approved deterministic mappings may be promoted after evidence review",
    }
    (output_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    (output_dir / "summary.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False))


if __name__ == "__main__":
    main()
