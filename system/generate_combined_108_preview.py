"""Combine the already-gated 66-row and 42-row previews without mutation."""
from __future__ import annotations

import csv
import hashlib
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parent
FORMAT_DIR = PROJECT_ROOT / "pilots" / "HD_SAAS" / "next_ai_format_preview"
QUESTION_DIR = PROJECT_ROOT / "pilots" / "HD_SAAS" / "terminal_question_preview"
OUTPUT_DIR = PROJECT_ROOT / "pilots" / "HD_SAAS" / "combined_108_preview"
OUTPUT_CSV = OUTPUT_DIR / "rewrite_preview.csv"
MANIFEST_JSON = OUTPUT_DIR / "manifest.json"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def main() -> None:
    format_manifest = json.loads((FORMAT_DIR / "manifest.json").read_text(encoding="utf-8"))
    format_replay = json.loads((FORMAT_DIR / "replay_manifest.json").read_text(encoding="utf-8"))
    question_manifest = json.loads((QUESTION_DIR / "manifest.json").read_text(encoding="utf-8"))
    question_replay = json.loads((QUESTION_DIR / "replay_manifest.json").read_text(encoding="utf-8"))
    if format_manifest.get("preview_rows") != 66 or format_replay.get("status") != "passed" or format_replay.get("pass_count") != 131:
        raise SystemExit("66-row format gate failed.")
    if question_manifest.get("preview_rows") != 42 or question_replay.get("status") != "passed" or question_replay.get("pass_count") != 42:
        raise SystemExit("42-row terminal question gate failed.")
    format_rows = read_rows(FORMAT_DIR / "safe_punctuation_formal_preview.csv")
    question_rows = read_rows(QUESTION_DIR / "rewrite_preview.csv")
    rows = format_rows + question_rows
    ids = [row["CANDIDATE_ID"] for row in rows]
    if len(rows) != 108 or len(set(ids)) != 108:
        raise SystemExit(f"Combined preview must have 108 unique rows, found {len(rows)}/{len(set(ids))}")
    identities = [(row["SOURCE_SCHEMA"], row["SITEID"], row["ASSETNUM"]) for row in rows]
    if len(set(identities)) != 108:
        raise SystemExit("Combined preview contains duplicate source identities.")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    columns = list(format_rows[0].keys())
    for row in question_rows:
        for column in columns:
            row.setdefault(column, "")
    with OUTPUT_CSV.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)
    manifest = {
        "preview_id": f"combined-108-preview-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(), "preview_rows": 108,
        "source_previews": [{"file": str(FORMAT_DIR / "safe_punctuation_formal_preview.csv"), "rows": 66, "replay_id": format_replay["replay_id"]}, {"file": str(QUESTION_DIR / "rewrite_preview.csv"), "rows": 42, "replay_id": question_replay["replay_id"]}],
        "rule_versions": [format_manifest["rule_version"], question_manifest["rule_version"]],
        "by_rule": {"safe_punctuation": 66, "terminal_question_mark": 42},
        "by_site": dict(sorted(Counter(row["SITEID"] for row in rows).items())),
        "preview_file": str(OUTPUT_CSV), "preview_sha256": sha256(OUTPUT_CSV),
        "source_write": False, "formal_publication": False,
        "status": "combined_preview_ready_for_approval",
        "internal_question_marks_excluded": 477,
    }
    MANIFEST_JSON.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False))


if __name__ == "__main__":
    main()
