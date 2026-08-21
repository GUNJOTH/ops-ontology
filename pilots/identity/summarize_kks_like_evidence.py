"""Summarize KKS-like observations without decoding or rewriting them."""
from __future__ import annotations

import argparse
import csv
import json
import pathlib
from collections import Counter
from datetime import datetime, timezone


def read(path: pathlib.Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample-dir", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()
    sample_dir = pathlib.Path(args.sample_dir).resolve()
    output_dir = pathlib.Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=False)
    summary: dict[str, object] = {}
    for schema in ("HD_SAAS", "XNY_SAAS"):
        rows = read(sample_dir / f"{schema.lower()}_semantic_sample.csv")
        candidates = [row for row in rows if row.get("kks_like_candidate")]
        observations = []
        for row in candidates:
            location = row.get("kks_like_candidate", "")
            observations.append({
                "source_schema": schema,
                "site_id": row.get("site_id", ""),
                "asset_number": row.get("asset_number", ""),
                "location_candidate": location,
                "observed_prefix_2": location[:2],
                "observed_prefix_4": location[:4],
                "original_description": row.get("original_description", ""),
                "classification_description": row.get("classification_description", ""),
                "confirmation_status": "unconfirmed",
                "rewrite_allowed": "false",
            })
        output = output_dir / f"{schema.lower()}_kks_like_observations.csv"
        fields = list(observations[0]) if observations else [
            "source_schema", "site_id", "asset_number", "location_candidate",
            "observed_prefix_2", "observed_prefix_4", "original_description",
            "classification_description", "confirmation_status", "rewrite_allowed",
        ]
        with output.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(observations)
        summary[schema] = {
            "sample_rows": len(rows),
            "kks_like_count": len(observations),
            "site_count": len({row["site_id"] for row in observations}),
            "prefix_2_counts": dict(Counter(row["observed_prefix_2"] for row in observations)),
            "file": output.name,
            "decoded": False,
        }
    report = {
        "run_id": f"kks-like-evidence-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}",
        "sample_dir": str(sample_dir),
        "summary": summary,
        "policy": "KKS-like shape is evidence only; no bit-part decoding or name rewrite",
        "source_write": False,
        "formal_publication": False,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    (output_dir / "manifest.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
