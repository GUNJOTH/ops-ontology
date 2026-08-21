"""Build per-cluster previews and deterministic replay evidence without approval."""
from __future__ import annotations

import argparse
import csv
import json
import pathlib
import re
import unicodedata
from collections import defaultdict
from datetime import datetime, timezone

from safe_convert import to_int


def normalize(value: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value or "")).strip()


def parse_signature(signature: str) -> list[dict[str, str]]:
    pairs = []
    if signature == "contextual_or_composed_change":
        return pairs
    for item in signature.split("|"):
        codepoint, target = item.split("->", 1)
        pairs.append({
            "codepoint": codepoint,
            "from": chr(int(codepoint[2:], 16)),
            "to": target,
        })
    return pairs


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--clusters-file", required=True)
    parser.add_argument("--assignments-file", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()
    clusters_file = pathlib.Path(args.clusters_file).resolve()
    assignments_file = pathlib.Path(args.assignments_file).resolve()
    output_dir = pathlib.Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=False)
    preview_dir = output_dir / "previews"
    preview_dir.mkdir()

    cluster_meta: dict[str, dict[str, str]] = {}
    with clusters_file.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            cluster_meta[row["cluster_id"]] = row
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    with assignments_file.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            grouped[row["cluster_id"]].append(row)

    rule_candidates = []
    cluster_replays = []
    preview_fields = [
        "cluster_id", "source_schema", "site_id", "asset_number",
        "original_description", "proposed_normalized_description",
        "replay_proposed_description", "replay_status", "source_row_hash",
    ]
    for cluster_id, rows in sorted(grouped.items()):
        meta = cluster_meta[cluster_id]
        pairs = parse_signature(meta["pattern_signature"])
        pass_count = 0
        fail_count = 0
        preview_path = preview_dir / f"{cluster_id}.csv"
        with preview_path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=preview_fields)
            writer.writeheader()
            for row in rows:
                replay_proposed = normalize(row["original_description"])
                status = "pass" if replay_proposed == row["proposed_normalized_description"] else "fail"
                if status == "pass":
                    pass_count += 1
                else:
                    fail_count += 1
                writer.writerow({
                    "cluster_id": cluster_id,
                    "source_schema": row["source_schema"],
                    "site_id": row["site_id"],
                    "asset_number": row["asset_number"],
                    "original_description": row["original_description"],
                    "proposed_normalized_description": row["proposed_normalized_description"],
                    "replay_proposed_description": replay_proposed,
                    "replay_status": status,
                    "source_row_hash": row["source_row_hash"],
                })
        rule_candidates.append({
            "clusterId": cluster_id,
            "sourceSchema": meta["source_schema"],
            "title": "字符规范化候选：" + meta["pattern_readable"],
            "decision": "needs_review",
            "operation": "replace_exact_character_set_candidate" if pairs else "needs_review",
            "parameters": {"pairs": pairs},
            "scope": {"sourceSchema": meta["source_schema"], "sites": []},
            "evidence": {"matchedCount": to_int(meta.get("count")), "siteCount": to_int(meta.get("site_count")), "patternSignature": meta["pattern_signature"]},
            "confidence": 0.0,
            "riskLevel": "high",
            "agentStatus": "pending_or_unavailable",
            "requiresAgentOrHumanConfirmation": True,
            "sourceWrite": False,
            "formalPublication": False,
        })
        cluster_replays.append({
            "clusterId": cluster_id,
            "sourceSchema": meta["source_schema"],
            "inputCount": len(rows),
            "passCount": pass_count,
            "failCount": fail_count,
            "previewFile": f"previews/{preview_path.name}",
            "replayStatus": "passed" if fail_count == 0 else "failed",
            "approvalStatus": "pending_agent_or_human_review",
        })

    (output_dir / "cluster_rule_candidates.json").write_text(json.dumps(rule_candidates, ensure_ascii=False, indent=2), encoding="utf-8")
    (output_dir / "cluster_replay_results.json").write_text(json.dumps(cluster_replays, ensure_ascii=False, indent=2), encoding="utf-8")
    report = {
        "run_id": f"semantic-cluster-rule-previews-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}",
        "clusters_file": str(clusters_file),
        "assignments_file": str(assignments_file),
        "cluster_count": len(cluster_replays),
        "input_count": sum(item["inputCount"] for item in cluster_replays),
        "replay_pass_count": sum(item["passCount"] for item in cluster_replays),
        "replay_fail_count": sum(item["failCount"] for item in cluster_replays),
        "rule_candidate_count": len(rule_candidates),
        "rule_candidate_status": "all_pending_agent_or_human_confirmation",
        "preview_dir": "previews",
        "source_write": False,
        "formal_publication": False,
        "next_gate": "agent or human confirmation per cluster, then approval-specific replay and publication preview",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    (output_dir / "manifest.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
