"""Audit the low-risk whitespace-only full preview without publishing it."""
from __future__ import annotations

import argparse
import csv
import json
import pathlib
import re
import unicodedata
from collections import Counter
from datetime import datetime, timezone


def normalize(value: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value or "")).strip()


def no_whitespace(value: str) -> str:
    return re.sub(r"\s+", "", value or "")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--partition-dir", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    partition_dir = pathlib.Path(args.partition_dir).resolve()
    findings: list[dict[str, str]] = []
    by_schema: dict[str, Counter[str]] = {"HD_SAAS": Counter(), "XNY_SAAS": Counter()}
    identities: set[tuple[str, str, str]] = set()
    sites_by_schema: dict[str, set[str]] = {"HD_SAAS": set(), "XNY_SAAS": set()}
    total = 0
    for schema in ("HD_SAAS", "XNY_SAAS"):
        path = partition_dir / f"{schema.lower()}_safe_whitespace_only.csv"
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                total += 1
                by_schema[schema]["rows"] += 1
                key = (row.get("source_schema", ""), row.get("site_id", ""), row.get("asset_number", ""))
                if key in identities:
                    findings.append({"schema": schema, "reason": "DUPLICATE_IDENTITY", "identity": "|".join(key)})
                identities.add(key)
                if row.get("source_schema") != schema:
                    findings.append({"schema": schema, "reason": "SOURCE_SCOPE_MISMATCH", "identity": "|".join(key)})
                original = row.get("original_description", "")
                proposed = row.get("proposed_normalized_description", "")
                if no_whitespace(original) != no_whitespace(proposed):
                    findings.append({"schema": schema, "reason": "NON_WHITESPACE_CHANGE", "identity": "|".join(key)})
                if normalize(original) != proposed:
                    findings.append({"schema": schema, "reason": "NON_REPLAYABLE", "identity": "|".join(key)})
                if normalize(proposed) != proposed:
                    findings.append({"schema": schema, "reason": "NON_IDEMPOTENT", "identity": "|".join(key)})
                sites_by_schema[schema].add(row.get("site_id", ""))
    for schema in ("HD_SAAS", "XNY_SAAS"):
        by_schema[schema]["sites"] = len(sites_by_schema[schema])
    report = {
        "run_id": f"safe-whitespace-preview-audit-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}",
        "partition_dir": str(partition_dir),
        "row_count": total,
        "by_schema": {schema: dict(counts) for schema, counts in by_schema.items()},
        "identity_count": len(identities),
        "finding_count": len(findings),
        "findings": findings[:500],
        "status": "passed" if not findings else "failed",
        "eligible_for_rule_review": not findings,
        "source_write": False,
        "formal_publication": False,
        "next_gate": "representative review before rule confirmation",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    output = pathlib.Path(args.output).resolve()
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    if findings:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
