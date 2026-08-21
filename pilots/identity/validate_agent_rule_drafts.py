"""Validate AI rule drafts against local cluster evidence before previewing them."""
from __future__ import annotations

import argparse
import csv
import json
import pathlib
import unicodedata
from datetime import datetime, timezone

from safe_convert import to_float, to_int


def expected_pairs(signature: str) -> set[tuple[str, str]]:
    if signature == "contextual_or_composed_change":
        return set()
    pairs = set()
    for item in signature.split("|"):
        codepoint, target = item.split("->", 1)
        pairs.add((chr(int(codepoint[2:], 16)), target))
    return pairs


def allowed_source_chars(signature: str) -> set[str]:
    return {source for source, _ in expected_pairs(signature)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--drafts-file", required=True)
    parser.add_argument("--clusters-file", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    drafts = json.loads(pathlib.Path(args.drafts_file).read_text(encoding="utf-8"))
    with pathlib.Path(args.clusters_file).open("r", encoding="utf-8-sig", newline="") as handle:
        clusters = {row["cluster_id"]: row for row in csv.DictReader(handle)}
    findings: list[dict[str, object]] = []
    warnings: list[dict[str, object]] = []
    valid_proposals = []
    for draft in drafts:
        cluster_id = str(draft.get("clusterId") or "")
        decision = str(draft.get("decision") or "")
        cluster = clusters.get(cluster_id)
        if not cluster:
            findings.append({"clusterId": cluster_id, "reason": "UNKNOWN_CLUSTER"})
            continue
        if decision != "propose_rule":
            continue
        parameters = draft.get("parameters") or {}
        source = str(parameters.get("from") or "") if isinstance(parameters, dict) else ""
        target = str(parameters.get("to") or "") if isinstance(parameters, dict) else ""
        confidence = to_float(draft.get("confidence"))
        reasons = []
        if not source or not target:
            reasons.append("MISSING_EXPLICIT_FROM_TO")
        if any(char.isspace() for char in source):
            reasons.append("FROM_CONTAINS_WHITESPACE")
        if not set(source).issubset(allowed_source_chars(cluster["pattern_signature"])):
            reasons.append("FROM_CONTAINS_CHARACTER_OUTSIDE_CLUSTER_EVIDENCE")
        if unicodedata.normalize("NFKC", source) != target:
            reasons.append("FROM_TO_NOT_NFKC_REPLAYABLE")
        if confidence < 0.85:
            reasons.append("CONFIDENCE_BELOW_0_85")
        if reasons:
            findings.append({"clusterId": cluster_id, "reason": "|".join(reasons), "from": source, "to": target})
            continue
        if len(source) > 1 or len(expected_pairs(cluster["pattern_signature"])) > 1:
            warnings.append({
                "clusterId": cluster_id,
                "reason": "COMPOSITE_EXACT_REPLACEMENT_RULE",
                "sourceSchema": cluster["source_schema"],
                "mappingCount": len(expected_pairs(cluster["pattern_signature"])),
            })
        valid_proposals.append({
            "clusterId": cluster_id,
            "sourceSchema": cluster["source_schema"],
            "from": source,
            "to": target,
            "confidence": confidence,
            "reason": draft.get("reason", ""),
            "matchedClusterCount": to_int(cluster["count"]),
        })
    report = {
        "run_id": f"agent-rule-draft-validation-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}",
        "drafts_file": str(pathlib.Path(args.drafts_file).resolve()),
        "cluster_count": len(clusters),
        "draft_count": len(drafts),
        "propose_rule_count": sum(str(item.get("decision") or "") == "propose_rule" for item in drafts),
        "valid_proposal_count": len(valid_proposals),
        "warning_count": len(warnings),
        "finding_count": len(findings),
        "findings": findings,
        "warnings": warnings,
        "valid_proposals": valid_proposals,
        "status": "passed" if not findings else "failed",
        "source_write": False,
        "formal_publication": False,
        "next_gate": "generate per-rule preview and replay only for valid proposals",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    output = pathlib.Path(args.output).resolve()
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    if findings:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
