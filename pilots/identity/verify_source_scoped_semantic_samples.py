"""Verify source-scoped semantic preview samples without changing them."""
from __future__ import annotations

import argparse
import csv
import json
import pathlib
import re
import unicodedata


def normalize(value: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value or "")).strip()


def read(path: pathlib.Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample-dir", required=True)
    args = parser.parse_args()
    root = pathlib.Path(args.sample_dir).resolve()
    findings: list[str] = []
    summary: dict[str, object] = {}
    for schema in ("HD_SAAS", "XNY_SAAS"):
        path = root / f"{schema.lower()}_semantic_sample.csv"
        rows = read(path)
        keys = [(row.get("source_schema", ""), row.get("site_id", ""), row.get("asset_number", "")) for row in rows]
        if any(not all(key) for key in keys):
            findings.append(f"{schema}:blank_identity")
        if len(keys) != len(set(keys)):
            findings.append(f"{schema}:duplicate_identity")
        if any(row.get("source_schema") != schema for row in rows):
            findings.append(f"{schema}:source_scope_mismatch")
        if any(row.get("change_type") not in {"unchanged", "safe_whitespace_nfkc_preview"} for row in rows):
            findings.append(f"{schema}:unsupported_change_type")
        if any(row.get("proposed_normalized_description") != normalize(row.get("original_description", "")) for row in rows):
            findings.append(f"{schema}:normalization_not_replayable")
        if any(normalize(row.get("proposed_normalized_description", "")) != row.get("proposed_normalized_description", "") for row in rows):
            findings.append(f"{schema}:normalization_not_idempotent")
        if any(row.get("kks_confirmation_status") == "confirmed" for row in rows):
            findings.append(f"{schema}:unconfirmed_kks_marked_confirmed")
        summary[schema] = {
            "rows": len(rows),
            "sites": len({row.get("site_id") for row in rows}),
            "changed_preview_rows": sum(row.get("change_type") != "unchanged" for row in rows),
            "kks_like_rows": sum(bool(row.get("kks_like_candidate")) for row in rows),
        }
    report = {
        "status": "passed" if not findings else "failed",
        "findings": findings,
        "summary": summary,
        "preview_only": True,
        "source_write": False,
        "formal_publication": False,
    }
    (root / "verification.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    if findings:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
