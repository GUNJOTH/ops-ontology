"""Generate a read-only before/after preview for active format rules."""
from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parent
DB = ROOT / "data" / "semantic_workflow.sqlite3"
INPUT = PROJECT_ROOT / "pilots" / "HD_SAAS" / "quality" / "hd_quality_candidates.csv"
OUTPUT_DIR = PROJECT_ROOT / "pilots" / "HD_SAAS" / "format_rule_preview"
PREVIEW_CSV = OUTPUT_DIR / "format_rule_rewrite_preview.csv"
MANIFEST_JSON = OUTPUT_DIR / "manifest.json"
SUMMARY_JSON = OUTPUT_DIR / "summary.json"
RULE_VERSION = "sample-learned-20260812-v1"
RULE_KEYS = (
    "format.fullwidth_parenthesis_to_ascii",
    "format.fullwidth_comma_to_ascii",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_context(value: object) -> dict[str, object]:
    """Return an isolated empty context for malformed optional source JSON."""
    if not value:
        return {}
    try:
        parsed = json.loads(str(value))
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def transform(description: str) -> tuple[str, list[str]]:
    result = description
    rules: list[str] = []
    if "（" in result or "）" in result:
        result = result.replace("（", "(").replace("）", ")")
        rules.append("format.fullwidth_parenthesis_to_ascii")
    if "，" in result:
        result = result.replace("，", ",")
        rules.append("format.fullwidth_comma_to_ascii")
    return result, rules


def main() -> None:
    if not INPUT.exists():
        raise SystemExit(f"Frozen quality input does not exist: {INPUT}")

    connection = sqlite3.connect(DB)
    connection.row_factory = sqlite3.Row
    active = connection.execute(
        "SELECT rule_key,status,version FROM terminology_rule WHERE rule_key IN (?,?) ORDER BY rule_key",
        RULE_KEYS,
    ).fetchall()
    connection.close()
    if len(active) != len(RULE_KEYS) or any(row["status"] != "active" for row in active):
        raise SystemExit("Both format rules must be active before generating the preview.")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    columns = [
        "SITEID", "ASSETNUM", "ASSETID", "CANDIDATE_ID", "SOURCE_SNAPSHOT_ID",
        "SOURCE_ROW_HASH", "CONTEXT_HASH", "ORIGINAL_DESCRIPTION", "PROPOSED_DESCRIPTION",
        "APPLIED_RULE_KEYS", "LOCATION_CODE", "LOCATION_DESCRIPTION", "LOCATION_PARENT",
        "CLASSSTRUCTURE_DESCRIPTION", "CLASSIFICATION_DESCRIPTION", "CONTEXT_STATUS",
        "PREVIEW_STATUS",
    ]
    total = 0
    impacted = 0
    parenthesis_rows = 0
    comma_rows = 0
    by_site: Counter[str] = Counter()
    by_rule: Counter[str] = Counter()
    examples: list[dict[str, str]] = []
    with INPUT.open(encoding="utf-8-sig", newline="") as source, PREVIEW_CSV.open("w", encoding="utf-8-sig", newline="") as target:
        reader = csv.DictReader(source)
        writer = csv.DictWriter(target, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in reader:
            total += 1
            original = row.get("ORIGINAL_DESCRIPTION") or ""
            context = parse_context(row.get("CONTEXT_JSON"))
            proposed, rules = transform(original)
            if not rules:
                continue
            impacted += 1
            by_site[row.get("SITEID") or ""] += 1
            for rule_key in rules:
                by_rule[rule_key] += 1
            if "format.fullwidth_parenthesis_to_ascii" in rules:
                parenthesis_rows += 1
            if "format.fullwidth_comma_to_ascii" in rules:
                comma_rows += 1
            preview = {
                "SITEID": row.get("SITEID") or "",
                "ASSETNUM": row.get("ASSETNUM") or "",
                "ASSETID": row.get("ASSETID") or "",
                "CANDIDATE_ID": row.get("CANDIDATE_ID") or "",
                "SOURCE_SNAPSHOT_ID": row.get("SOURCE_SNAPSHOT_ID") or "",
                "SOURCE_ROW_HASH": row.get("SOURCE_ROW_HASH") or "",
                "CONTEXT_HASH": row.get("CONTEXT_HASH") or "",
                "ORIGINAL_DESCRIPTION": original,
                "PROPOSED_DESCRIPTION": proposed,
                "APPLIED_RULE_KEYS": "|".join(rules),
                "LOCATION_CODE": (context.get("location") if isinstance(context.get("location"), dict) else {}).get("LOCATION", ""),
                "LOCATION_DESCRIPTION": row.get("LOCATION_DESCRIPTION") or "",
                "LOCATION_PARENT": row.get("LOCATION_PARENT") or "",
                "CLASSSTRUCTURE_DESCRIPTION": row.get("CLASSSTRUCTURE_DESCRIPTION") or "",
                "CLASSIFICATION_DESCRIPTION": row.get("CLASSIFICATION_DESCRIPTION") or "",
                "CONTEXT_STATUS": row.get("CONTEXT_STATUS") or "",
                "PREVIEW_STATUS": "ready_for_confirmation",
            }
            writer.writerow(preview)
            if len(examples) < 20:
                examples.append({
                    "site": preview["SITEID"],
                    "asset": preview["ASSETNUM"],
                    "original": original,
                    "proposed": proposed,
                    "rules": preview["APPLIED_RULE_KEYS"],
                })

    input_hash = sha256(INPUT)
    generated_at = utc_now()
    summary = {
        "input_rows": total,
        "impacted_rows": impacted,
        "unchanged_rows": total - impacted,
        "parenthesis_rule_rows": parenthesis_rows,
        "comma_rule_rows": comma_rows,
        "by_site": dict(sorted(by_site.items())),
        "by_rule": dict(sorted(by_rule.items())),
        "examples": examples,
    }
    manifest = {
        "preview_id": f"format-preview-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}",
        "generated_at_utc": generated_at,
        "source_file": str(INPUT),
        "source_sha256": input_hash,
        "source_rows": total,
        "source_write": False,
        "formal_publication": False,
        "rule_version": RULE_VERSION,
        "rule_keys": list(RULE_KEYS),
        "rule_status": "active",
        "preview_file": str(PREVIEW_CSV),
        "preview_rows": impacted,
        "preview_scope": "changed rows only; source and formal result layers untouched",
        "summary": summary,
    }
    MANIFEST_JSON.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    SUMMARY_JSON.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"manifest": str(MANIFEST_JSON), "preview": str(PREVIEW_CSV), **summary}, ensure_ascii=False))


if __name__ == "__main__":
    main()
