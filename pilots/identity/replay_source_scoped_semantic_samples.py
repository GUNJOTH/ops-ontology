"""Replay source-scoped semantic preview rules without publishing changes."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import pathlib
import re
import unicodedata
from collections import Counter
from datetime import datetime, timezone


def normalize(value: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value or "")).strip()


def read(path: pathlib.Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def identity_hash(row: dict[str, str]) -> str:
    raw = "|".join((row.get("source_schema", ""), row.get("site_id", ""), row.get("asset_number", "")))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def input_path(sample_dir: pathlib.Path, schema: str) -> pathlib.Path:
    """Accept both the bounded sample and the full-impact preview layout."""
    candidates = (
        sample_dir / f"{schema.lower()}_semantic_sample.csv",
        sample_dir / f"{schema.lower()}_full_semantic_preview.csv",
    )
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise FileNotFoundError("no semantic input for %s in %s" % (schema, sample_dir))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample-dir", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()
    sample_dir = pathlib.Path(args.sample_dir).resolve()
    output_dir = pathlib.Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=False)
    all_rows: list[dict[str, str]] = []
    findings: list[dict[str, str]] = []
    by_schema: dict[str, dict[str, int]] = {}
    input_files: dict[str, str] = {}
    for schema in ("HD_SAAS", "XNY_SAAS"):
        source_path = input_path(sample_dir, schema)
        input_files[schema] = source_path.name
        rows = read(source_path)
        seen: set[tuple[str, str, str]] = set()
        counts = Counter()
        for row in rows:
            key = (row.get("source_schema", ""), row.get("site_id", ""), row.get("asset_number", ""))
            recomputed = normalize(row.get("original_description", ""))
            reasons: list[str] = []
            if key in seen:
                reasons.append("DUPLICATE_SOURCE_IDENTITY")
            seen.add(key)
            if row.get("source_schema") != schema:
                reasons.append("SOURCE_SCOPE_MISMATCH")
            if row.get("proposed_normalized_description") != recomputed:
                reasons.append("NON_REPLAYABLE_NORMALIZATION")
            if normalize(recomputed) != recomputed:
                reasons.append("NON_IDEMPOTENT_NORMALIZATION")
            if row.get("kks_confirmation_status") == "confirmed":
                reasons.append("UNSUPPORTED_KKS_CONFIRMATION")
            decision = "pass" if not reasons else "fail"
            counts[decision] += 1
            all_rows.append({
                "source_schema": schema,
                "site_id": row.get("site_id", ""),
                "asset_number": row.get("asset_number", ""),
                "source_row_hash": row.get("source_row_hash", ""),
                "identity_hash": identity_hash(row),
                "original_description": row.get("original_description", ""),
                "proposed_normalized_description": row.get("proposed_normalized_description", ""),
                "change_type": row.get("change_type", ""),
                "decision": decision,
                "reason_codes": "|".join(reasons),
            })
            if reasons:
                findings.append({"schema": schema, "site_id": key[1], "asset_number": key[2], "reason_codes": "|".join(reasons)})
        by_schema[schema] = dict(counts)

    result_csv = output_dir / "replay_results.csv"
    fields = list(all_rows[0]) if all_rows else []
    with result_csv.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(all_rows)
    report = {
        "run_id": f"source-scoped-semantic-replay-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}",
        "sample_dir": str(sample_dir),
        "sample_count": len(all_rows),
        "input_files": input_files,
        "pass_count": sum(1 for row in all_rows if row["decision"] == "pass"),
        "fail_count": len(findings),
        "by_schema": by_schema,
        "findings": findings,
        "rule_version": "source-scoped-semantic-preview-v1",
        "preview_only": True,
        "source_write": False,
        "formal_publication": False,
        "next_gate": "manual confirmation of changed rows before rule activation",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    (output_dir / "replay_manifest.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    if findings:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
