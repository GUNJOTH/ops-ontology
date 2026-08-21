"""Create a pending approval queue from replay-passed AI rule previews."""
from __future__ import annotations

import argparse
import json
import pathlib
from datetime import datetime, timezone

from safe_convert import to_int


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--validation-file", required=True)
    parser.add_argument("--preview-results-file", required=True)
    parser.add_argument("--safe-audit-file", required=True)
    parser.add_argument("--safe-preview-dir", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    validation = json.loads(pathlib.Path(args.validation_file).read_text(encoding="utf-8"))
    preview_results = json.loads(pathlib.Path(args.preview_results_file).read_text(encoding="utf-8"))
    safe_audit = json.loads(pathlib.Path(args.safe_audit_file).read_text(encoding="utf-8"))
    passed = [item for item in preview_results if item.get("replayStatus") == "passed"]
    failed = [item for item in preview_results if item.get("replayStatus") != "passed"]
    queue = []
    if safe_audit.get("status") == "passed":
        queue.append({
            "queueId": "rule-approval-safe-whitespace-trim-collapse-v1",
            "ruleKey": "safe-whitespace-trim-collapse-v1",
            "ruleType": "deterministic",
            "sourceSchema": ["HD_SAAS", "XNY_SAAS"],
            "from": "whitespace variants",
            "to": "single normalized whitespace",
            "previewCount": to_int(safe_audit.get("row_count"), 0),
            "previewFiles": [
                str(pathlib.Path(args.safe_preview_dir).resolve() / "hd_saas_safe_whitespace_only.csv"),
                str(pathlib.Path(args.safe_preview_dir).resolve() / "xny_saas_safe_whitespace_only.csv"),
            ],
            "replayStatus": "passed",
            "approvalStatus": "pending_human_approval",
            "enabled": False,
            "formalPublication": False,
            "sourceWrite": False,
        })
    for item in passed:
        queue.append({
            "queueId": f"rule-approval-{item['ruleIndex']:03d}-{item['clusterId']}",
            "clusterId": item["clusterId"],
            "sourceSchema": item["sourceSchema"],
            "from": item["from"],
            "to": item["to"],
            "previewCount": item["previewCount"],
            "previewFile": item["previewFile"],
            "replayStatus": item["replayStatus"],
            "approvalStatus": "pending_human_approval",
            "enabled": False,
            "formalPublication": False,
            "sourceWrite": False,
        })
    report = {
        "run_id": f"semantic-rule-approval-queue-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}",
        "validation_file": str(pathlib.Path(args.validation_file).resolve()),
        "preview_results_file": str(pathlib.Path(args.preview_results_file).resolve()),
        "safe_audit_file": str(pathlib.Path(args.safe_audit_file).resolve()),
        "queue_count": len(queue),
        "blocked_preview_count": len(failed),
        "validation_findings": len(validation.get("findings", [])),
        "queue": queue,
        "blocked": failed,
        "approval_policy": "replay-passed rules only; separate human confirmation required; no automatic enable or publication",
        "source_write": False,
        "formal_publication": False,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    output = pathlib.Path(args.output).resolve()
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "run_id": report["run_id"],
        "queue_count": report["queue_count"],
        "blocked_preview_count": report["blocked_preview_count"],
        "source_write": False,
        "formal_publication": False,
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
