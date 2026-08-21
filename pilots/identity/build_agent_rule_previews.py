"""Build isolated previews and replay checks for validated AI rule proposals."""
from __future__ import annotations

import argparse
import csv
import json
import pathlib
from collections import defaultdict
from datetime import datetime, timezone


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--validation-file", required=True)
    parser.add_argument("--assignments-file", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()
    validation_path = pathlib.Path(args.validation_file).resolve()
    assignments_path = pathlib.Path(args.assignments_file).resolve()
    output_dir = pathlib.Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=False)
    preview_dir = output_dir / "previews"
    preview_dir.mkdir()

    validation = json.loads(validation_path.read_text(encoding="utf-8"))
    proposals = validation.get("valid_proposals", [])
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    with assignments_path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            grouped[row["cluster_id"]].append(row)

    results = []
    fields = [
        "cluster_id", "source_schema", "site_id", "asset_number",
        "original_description", "rule_proposed_description",
        "source_row_hash", "match_status", "idempotence_status",
    ]
    for index, proposal in enumerate(proposals, start=1):
        cluster_id = proposal["clusterId"]
        source = proposal["from"]
        target = proposal["to"]
        rows = grouped.get(cluster_id, [])
        match_count = 0
        not_match_count = 0
        idempotence_fail_count = 0
        preview_path = preview_dir / f"{index:03d}_{cluster_id}.csv"
        with preview_path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            for row in rows:
                original = row.get("original_description", "")
                if source in original:
                    proposed = original.replace(source, target)
                    match_status = "matched"
                    match_count += 1
                else:
                    proposed = original
                    match_status = "not_matched"
                    not_match_count += 1
                if proposed.replace(source, target) != proposed:
                    idempotence_status = "failed"
                    idempotence_fail_count += 1
                else:
                    idempotence_status = "passed"
                writer.writerow({
                    "cluster_id": cluster_id,
                    "source_schema": row.get("source_schema", ""),
                    "site_id": row.get("site_id", ""),
                    "asset_number": row.get("asset_number", ""),
                    "original_description": original,
                    "rule_proposed_description": proposed,
                    "source_row_hash": row.get("source_row_hash", ""),
                    "match_status": match_status,
                    "idempotence_status": idempotence_status,
                })
        status = "passed" if rows and not_match_count == 0 and idempotence_fail_count == 0 else "failed"
        results.append({
            "ruleIndex": index,
            "clusterId": cluster_id,
            "sourceSchema": proposal["sourceSchema"],
            "from": source,
            "to": target,
            "inputCount": len(rows),
            "previewCount": match_count,
            "notMatchedCount": not_match_count,
            "idempotenceFailCount": idempotence_fail_count,
            "replayStatus": status,
            "previewFile": f"previews/{preview_path.name}",
            "approvalStatus": "pending_human_confirmation",
            "sourceWrite": False,
            "formalPublication": False,
        })

    report = {
        "run_id": f"agent-rule-previews-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}",
        "validation_file": str(validation_path),
        "proposal_count": len(proposals),
        "preview_count": sum(item["previewCount"] for item in results),
        "replay_pass_count": sum(item["replayStatus"] == "passed" for item in results),
        "replay_fail_count": sum(item["replayStatus"] == "failed" for item in results),
        "partial_match_rule_count": sum(item["notMatchedCount"] > 0 for item in results),
        "rule_results_file": "rule_preview_results.json",
        "preview_dir": "previews",
        "source_write": False,
        "formal_publication": False,
        "next_gate": "human confirmation of each replay-passed rule before approval",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    (output_dir / "rule_preview_results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    (output_dir / "manifest.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
